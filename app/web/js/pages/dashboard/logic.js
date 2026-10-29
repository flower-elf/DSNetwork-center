// 仪表盘的纯计算：账号筛选、可用性分档、积分与到期汇总、健康快照与出口密钥计数；只依赖 core/format.js 与账号页的积分函数。

import { barColor, compareText, fmtNumber, fmtCredit, fmtDate } from '../../core/format.js';
import { creditBarPct, validCredits } from '../accounts/logic.js';

/** 账号所属版本：存量账号没有 realm 字段时按国际版处理，与后端默认一致。 */
export function realmOf(account) {
  return account && account.realm ? account.realm : 'intl';
}

export function realmAccounts(accounts, realm) {
  return (accounts || []).filter((a) => realmOf(a) === realm);
}

/** 令牌剩余秒数；没有到期时间返回 null。 */
export function remainSeconds(account, now) {
  const at = Number(account && account.expiresAt) || 0;
  if (!at) return null;
  return Math.round(at - now / 1000);
}

export function isExpired(account, now) {
  const remain = remainSeconds(account, now);
  return remain !== null && remain <= 0;
}

/** 现在就能接流的账号：启用、令牌未过期、不在冷却、没被保留积分与当日限额挡下。 */
export function isReady(account, now) {
  return !!account.enabled
    && !isExpired(account, now)
    && !account.inCooldown
    && !account.reserveBlocked
    && !account.dailyLimitBlocked;
}

/** 可用性分档，顺序即优先级：已停用、令牌过期、当日额度已满、低于保留积分、冷却中、上次调用失败、可用。 */
export function availability(account, now) {
  if (!account.enabled) return { label: '已停用', tone: 'muted' };
  if (isExpired(account, now)) return { label: '令牌过期', tone: 'danger' };
  if (account.dailyLimitBlocked) return { label: '今日额度已满', tone: 'warning' };
  if (account.reserveBlocked) return { label: '低于保留积分', tone: 'warning' };
  if (account.inCooldown) return { label: '冷却中', tone: 'warning' };
  if (account.lastError) return { label: '上次调用失败', tone: 'warning' };
  return { label: '可用', tone: 'success' };
}

/** 账号总数、启用数、可用数与需处理数；需处理 = 总数减可用，停用的账号也在此列。 */
export function accountSummary(accounts, now) {
  const list = accounts || [];
  let enabled = 0;
  let ready = 0;
  for (const a of list) {
    if (a.enabled) enabled += 1;
    if (isReady(a, now)) ready += 1;
  }
  return { total: list.length, enabled, ready, attention: list.length - ready };
}

export function readyHint(summary) {
  if (!summary.total) return '';
  return summary.attention === 0 ? '全部正常' : summary.attention + ' 个需处理';
}

/** 余额汇总：total 只累加读得到的有效期内余额，low 统计低于保留积分的账号。 */
export function creditsSummary(accounts, now = Date.now()) {
  let total = 0;
  let known = 0;
  let low = 0;
  let reserve = 0;
  for (const a of accounts || []) {
    const credits = a.credits;
    const raw = credits && credits.remain != null ? Number(credits.remain) : NaN;
    if (!Number.isFinite(raw)) continue;
    const valid = validCredits(a, now);
    const remain = valid && Number.isFinite(valid.remain) ? valid.remain : raw;
    known += 1;
    total += remain;
    const threshold = Number(a.reserveCredits) || 0;
    if (threshold > 0) {
      reserve = Math.max(reserve, threshold);
      if (raw <= threshold) low += 1;
    }
  }
  return { total, known, low, reserve };
}

/** 一个账号的积分到期明细，按到期时刻升序；优先用后端算好的 creditExpiries，读不到时从套餐的 expireAt 推导。 */
export function accountExpiries(account) {
  const direct = account && account.creditExpiries;
  if (Array.isArray(direct) && direct.length) {
    return direct
      .map((e) => ({ at: Number(e.at) || 0, amount: Number(e.amount) || 0, name: e.name || '' }))
      .filter((e) => e.at > 0)
      .sort((a, b) => a.at - b.at);
  }
  const packages = (account && account.credits && account.credits.packages) || [];
  return packages
    .map((p) => ({ at: Number(p && p.expireAt) || 0, amount: Number(p && p.remain) || 0, name: (p && p.name) || '' }))
    .filter((e) => e.at > 0 && e.amount > 0)
    .sort((a, b) => a.at - b.at);
}

/** 全部账号里最近的一笔积分到期；已经过去的到期时刻不计。 */
export function nextCreditExpiry(accounts, now) {
  const nowSec = now / 1000;
  let best = null;
  for (const a of accounts || []) {
    for (const e of accountExpiries(a)) {
      if (e.at <= nowSec || e.amount <= 0) continue;
      if (!best || e.at < best.at) best = { at: e.at, amount: e.amount, name: e.name, uid: a.uid };
      break;
    }
  }
  return best;
}

/** 积分卡的提示，按优先级取一条：低于保留积分、最近一笔到期、覆盖账号数。 */
export function creditsHint(accounts, now) {
  const summary = creditsSummary(accounts);
  if (!summary.known) return '';
  if (summary.low > 0) return summary.low + ' 个账号低于保留积分';
  const next = nextCreditExpiry(accounts, now);
  if (next) return '最近一笔 ' + fmtCredit(next.amount) + ' 积分将于 ' + fmtDate(next.at) + ' 到期';
  return '覆盖 ' + summary.known + ' 个账号';
}

/** 快照里的排序权重：越小越先展示；停用是用户自己的决定，排在最后。 */
function snapshotRank(account, now) {
  if (isExpired(account, now)) return 0;
  if (account.dailyLimitBlocked || account.reserveBlocked || account.inCooldown) return 1;
  if (account.lastError) return 2;
  if (!account.enabled) return 4;
  return 3;
}

/** 健康快照展示的账号：需处理的在前，其余按令牌剩余时间升序，最多 limit 个；同一份数据渲染顺序一致。 */
export function snapshotAccounts(accounts, limit = 9, now = Date.now()) {
  return [...(accounts || [])]
    .sort((a, b) => {
      const ra = snapshotRank(a, now);
      const rb = snapshotRank(b, now);
      if (ra !== rb) return ra - rb;
      const ma = remainSeconds(a, now);
      const mb = remainSeconds(b, now);
      const va = ma === null ? Number.POSITIVE_INFINITY : ma;
      const vb = mb === null ? Number.POSITIVE_INFINITY : mb;
      if (va !== vb) return va - vb;
      return compareText(a.uid || '', b.uid || '');
    })
    .slice(0, limit);
}

// 快照标题行的档位顺序：可用排最前，其次是需要处理的原因，最后是已停用。
const AVAILABILITY_ORDER = ['可用', '冷却中', '上次调用失败', '低于保留积分', '今日额度已满', '令牌过期', '已停用'];

/** 各可用性档位的账号数，按固定顺序返回，只含非零档位。 */
export function availabilityCounts(accounts, now) {
  const counts = new Map();
  for (const a of accounts || []) {
    const hit = availability(a, now);
    const entry = counts.get(hit.label) || { label: hit.label, tone: hit.tone, count: 0 };
    entry.count += 1;
    counts.set(hit.label, entry);
  }
  return AVAILABILITY_ORDER
    .filter((label) => counts.has(label))
    .map((label) => counts.get(label));
}

/** 令牌剩余时长与文字颜色：1 小时内警告，6 小时内提示，其余常规；没有到期时间返回 null。 */
export function expiryVisual(account, now) {
  const remain = remainSeconds(account, now);
  if (remain === null) return null;
  const tone = remain <= 0 ? 'danger' : remain < 3600 ? 'warning' : remain < 6 * 3600 ? 'info' : 'success';
  return { remain, tone };
}

/** 健康快照的进度条：按有效期内可用积分占总额的比例取满格，颜色按比例分档，总额未知时返回 null。 */
export function creditBar(account, now = Date.now()) {
  const pct = creditBarPct(account, now);
  return pct === null ? null : { pct, color: barColor(pct) };
}

/** 积分展示分档：无数据灰、小于等于 0 红、低于保留积分琥珀、其余正常。只看有效期内余额，总额一并给出。 */
export function creditTone(account, now = Date.now()) {
  const valid = validCredits(account, now);
  const remain = valid ? valid.remain : NaN;
  if (!Number.isFinite(remain)) return { text: '—', total: null, tone: 'muted' };
  const total = valid.size > 0 ? fmtCredit(valid.size) : null;
  if (remain <= 0) return { text: fmtCredit(remain), total, tone: 'danger' };
  const threshold = Number(account.reserveCredits) || 0;
  if (threshold > 0 && remain <= threshold) return { text: fmtCredit(remain), total, tone: 'warning' };
  return { text: fmtCredit(remain), total, tone: '' };
}

/** 默认出口当前生效的密钥数：绑定该出口的启用密钥 + 未绑定版本的启用密钥。 */
export function keyEffect(keys, activeRealm) {
  const list = keys || [];
  const on = (k) => k && k.enabled !== false;
  const bound = list.filter((k) => on(k) && k.realm === activeRealm).length;
  const follow = list.filter((k) => on(k) && !k.realm).length;
  return { bound, follow, total: bound + follow };
}

/** 趋势窗口的合计：请求次数、失败次数与 Token 消耗。 */
export function trendSummary(buckets) {
  const list = buckets || [];
  const sum = (key) => list.reduce((acc, b) => acc + (Number(b[key]) || 0), 0);
  return { requests: sum('requests'), failures: sum('errors'), tokens: sum('total_tokens') };
}

export function usageHints(usage) {
  const errors = Number(usage && usage.errors) || 0;
  const credit = Number(usage && usage.credit) || 0;
  return {
    requestHint: errors > 0 ? '失败 ' + fmtNumber(errors) + ' 次' : '全部成功',
    tokenHint: '消耗 ' + fmtCredit(credit) + ' 积分',
  };
}
