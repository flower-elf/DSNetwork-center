// 账号今天的活动状态。国内版有两项：Buddy 加油站（签到）与成长计划的连登活跃上报；
// 国际版只有一项：今日活跃（每日活跃对话）。
//
// 事实来源是 GET /activity/history?range=today 的流水；账号自带的 lastCheckin /
// lastDailyChat / lastActivityReport 只在「今天确实做过、但这一版还没有历史行」时
// 补一个中性状态。没有记录不等于失败。

import { fmtDate } from '../../core/format.js';

export const BUDDY_LABEL = 'Buddy 加油站';

function startsToday(value, stamp) {
  return !!value && String(value).startsWith(stamp);
}

/** 流水按最新在前，每个 uid 只留最近一条。 */
export function latestByUid(rows) {
  const out = {};
  (rows || []).forEach((row) => {
    const uid = String((row && row.uid) || '');
    if (uid && !out[uid]) out[uid] = row;
  });
  return out;
}

function fromRow(row, label, okText, failText) {
  return {
    kind: row.ok ? 'ok' : 'failed',
    text: row.ok ? okText : failText,
    at: String(row.ts || '').replace('T', ' ').slice(0, 19),
    hint: String(row.message || ''),
  };
}

export function activityView(account, byUid, now = Date.now()) {
  const uid = String((account && account.uid) || '');
  const row = (byUid || {})[uid] || null;
  const stamp = fmtDate(now);

  if (account && account.realm === 'cn') {
    let gas = { kind: 'none', text: '未领', at: '', hint: '今天还没有 Buddy 加油站的记录' };
    if (row && row.task === 'checkin') gas = fromRow(row, BUDDY_LABEL, '已领', '失败');
    else if (startsToday(account.lastCheckin, stamp)) {
      gas = { kind: 'done', text: '已领', at: String(account.lastCheckin), hint: '今天已完成' };
    }
    const streak = startsToday(account.lastActivityReport, stamp)
      ? { kind: 'done', text: '已签到', at: String(account.lastActivityReport), hint: '连登活跃已上报' }
      : { kind: 'none', text: '未签到', at: '', hint: '今天还没有成长计划连登活跃的记录' };
    return { realm: 'cn', gas, streak };
  }

  let daily = { kind: 'none', text: '未打卡', at: '', hint: '今天还没有每日活跃的记录' };
  if (row && row.task === 'daily_chat') daily = fromRow(row, '今日活跃', '已完成', '失败');
  else if (startsToday(account.lastDailyChat, stamp)) {
    daily = { kind: 'done', text: '已完成', at: String(account.lastDailyChat), hint: '今天已完成' };
  }
  return { realm: 'intl', daily };
}

/** 这项活动今天算不算完成：流水成功或本地记下的完成时间都算。 */
export function isDone(status) {
  const item = status && (status.realm === 'cn' ? status.gas : status.daily);
  return !!item && (item.kind === 'ok' || item.kind === 'done');
}

/** 国际版今天还没打卡的已启用账号，顺序与账号列表一致。 */
export function pendingDailyChat(accounts, byUid, now = Date.now()) {
  return (accounts || []).filter((account) => account.realm === 'intl'
    && account.enabled && !isDone(activityView(account, byUid, now)));
}

/** 某个版本今天的完成情况：{total, done, pending: [账号]}。 */
export function todaySummary(accounts, byUid, realm, now = Date.now()) {
  const visible = (accounts || []).filter((account) => account.realm === realm && account.enabled);
  const pending = visible.filter((account) => !isDone(activityView(account, byUid, now)));
  return { total: visible.length, done: visible.length - pending.length, pending };
}

/** 国内版成长计划（连登活跃上报）今天的完成情况。 */
export function streakSummary(accounts, now = Date.now()) {
  const stamp = fmtDate(now);
  const visible = (accounts || []).filter((account) => account.realm === 'cn' && account.enabled);
  const done = visible.filter((account) => startsToday(account.lastActivityReport, stamp)).length;
  return { total: visible.length, done };
}

/** 悬停提示里的名单：昵称或 UID 前缀。 */
export function namesOf(accounts) {
  return (accounts || []).map((account) => account.nickname || String(account.uid || '').slice(0, 8));
}

/** 卡片上的「已领 x/y」文案。 */
export function summaryText(summary, doneWord, pendingWord) {
  if (!summary.total) return '无账号';
  if (summary.done === summary.total) return '今日' + doneWord + ' (' + summary.done + '/' + summary.total + ')';
  if (summary.done > 0) return '部分' + doneWord + ' (' + summary.done + '/' + summary.total + ')';
  return '今日' + pendingWord + ' (0/' + summary.total + ')';
}

/** 今日活动汇总卡的数值、色调与提示；账号页与任务页共用同一套口径。 */
export function summaryStat(summary, { doneWord, pendingWord }) {
  return {
    value: summaryText(summary, doneWord, pendingWord),
    tone: summary.total && summary.done === summary.total ? 'success' : 'warning',
    hint: summary.pending.length
      ? '待处理：' + namesOf(summary.pending).join('、')
      : '今天全部完成',
  };
}
