// 用量页的纯计算：时间范围的查询串、账号与密钥的用量行、模型性能矩阵的行与合计、筛选条件的生成与失效清理；只依赖 core/format.js，方便在 Node 里直接测试。

import { compareText } from '../../core/format.js';

export const RANGES = [
  { id: 'today', label: '今日' },
  { id: 'week', label: '本周' },
  { id: 'month', label: '本月' },
  { id: 'all', label: '全部' },
  { id: 'custom', label: '自定义区间' },
];

export function rangeLabel(range) {
  const hit = RANGES.find((r) => r.id === range);
  return hit ? hit.label : RANGES[0].label;
}

export const TREND_MODES = [
  { id: 'both', label: '两者' },
  { id: 'requests', label: '请求数' },
  { id: 'tokens', label: 'Token' },
];

/**
 * 趋势图的标签与序列：请求数（绿）在左轴、Token（蓝）在右轴，都不带填充；只画一条时占左轴，
 * 免得图上留下一整条没有数据的坐标轴。options.errors 打开后请求数之后追加一条失败数（红色虚线）。
 *
 * order 决定叠放次序：Chart.js 按 order 升序排好再从后往前画，所以 order 大的先画、被压在下面。
 * 两条折线走势接近时，如果都留默认的 0，请求数会因为排在后面而盖住 Token。
 */
export function trendSeries(mode, buckets, fmtNumber, fmtCompact, options = {}) {
  const list = buckets || [];
  const labels = list.map((b) => b.label || '');
  const requests = {
    label: '请求数', color: '--success', format: fmtNumber, order: 3,
    data: list.map((b) => Number(b.requests) || 0),
  };
  const failures = {
    label: '失败数', color: '--destructive', dashed: true, format: fmtNumber, order: 2,
    data: list.map((b) => Number(b.errors) || 0),
  };
  const tokens = {
    label: 'Token', color: '--info', format: fmtCompact, order: 1,
    data: list.map((b) => Number(b.total_tokens) || 0),
  };
  const withErrors = !!options.errors;
  if (mode === 'requests') return { labels, series: withErrors ? [requests, failures] : [requests] };
  if (mode === 'tokens') return { labels, series: [tokens] };
  return { labels, series: withErrors ? [requests, failures, Object.assign({ axis: 'y2' }, tokens)] : [requests, Object.assign({ axis: 'y2' }, tokens)] };
}

/** 统计口径：默认只看当前查看的版本，勾选「含两个版本」后合并统计。 */
export function scopeRealm(viewRealm, includeAll) {
  return includeAll ? 'all' : viewRealm;
}

export function scopeText(includeAll, viewRealm) {
  return includeAll ? '国内版与国际版合并' : (viewRealm === 'cn' ? '国内版' : '国际版');
}

/**
 * 三个取数端点共用同一个查询串：/usage、/usage/perf、/usage/analytics 与 /usage/trend 的窗口必须一致，
 * 否则卡片、表格与图各说各的。自定义区间只带上填了的那一侧，空的一侧表示不限。
 */
export function buildQuery({ realm, range, since = null, until = null }) {
  const parts = ['realm=' + encodeURIComponent(realm || 'all'), 'range=' + encodeURIComponent(range || 'today')];
  if (range === 'custom') {
    if (since) parts.push('since=' + encodeURIComponent(since));
    if (until) parts.push('until=' + encodeURIComponent(until));
  }
  return '?' + parts.join('&');
}

/** 趋势图的查询串：仪表盘按最近 N 天取，用量页跟随当前范围。days 传了就不带 range，服务端以 days 为准。 */
export function trendQuery({ realm, range = '', since = null, until = null, days = 0 }) {
  const parts = ['realm=' + encodeURIComponent(realm || 'all')];
  if (days) parts.push('days=' + encodeURIComponent(days));
  else if (range) parts.push('range=' + encodeURIComponent(range));
  if (!days && range === 'custom') {
    if (since) parts.push('since=' + encodeURIComponent(since));
    if (until) parts.push('until=' + encodeURIComponent(until));
  }
  return '?' + parts.join('&');
}

export function trendGranularity(range) {
  return range === 'today' ? '按小时，当日汇总' : '按天聚合';
}

/**
 * 账号用量行，取自 analytics.accounts 的 window 桶，也就是当前选中的范围。
 * 只有零调用的未归属账号会被丢掉：它没有对应的账号，列出来也点不开。
 */
export function accountRows(data) {
  const list = (data && data.accounts) || [];
  return list
    .filter((a) => {
      if (a.uid !== '(unattributed)') return true;
      const allTime = a.all_time || {};
      return ((a.window || {}).requests || 0) > 0 || (allTime.requests || 0) > 0;
    })
    .map((a) => {
      const win = a.window || {};
      const models = Object.entries(a.window_models || {})
        .map(([id, m]) => ({ id, requests: Number(m.requests) || 0, tokens: Number(m.tokens) || 0 }))
        .filter((m) => m.requests > 0 || m.tokens > 0)
        .sort((x, y) => y.tokens - x.tokens || y.requests - x.requests || compareText(x.id, y.id));
      return {
        uid: a.uid || '',
        nickname: a.nickname || a.uid || '',
        realm: a.realm || '',
        credits: a.credits || null,
        requests: Number(win.requests) || 0,
        errors: Number(win.errors) || 0,
        totalTokens: Number(win.total_tokens) || 0,
        credit: Number(win.credit) || 0,
        models,
      };
    })
    .sort((a, b) => b.totalTokens - a.totalTokens || b.requests - a.requests || compareText(a.uid, b.uid));
}

/** 把一组 Token 数换算成百分比，用最大余数法分配零头，保证结果加起来正好 100。 */
export function allocateShares(values) {
  const total = values.reduce((sum, v) => sum + Math.max(0, v), 0);
  if (!(total > 0)) return values.map(() => 0);
  const exact = values.map((v) => (Math.max(0, v) / total) * 100);
  const shares = exact.map((v) => Math.floor(v));
  let rest = 100 - shares.reduce((sum, v) => sum + v, 0);
  const order = exact
    .map((v, i) => ({ rem: v - shares[i], i }))
    .sort((a, b) => b.rem - a.rem || a.i - b.i);
  for (let i = 0; i < order.length && rest > 0; i += 1) {
    shares[order[i].i] += 1;
    rest -= 1;
  }
  return shares;
}

/** 有调用的密钥行，按窗口 Token 降序；`share` 是这一行占表内合计的百分比。 */
export function keyRows(data) {
  const rows = ((data && data.keys) || [])
    .map((k) => {
      const win = k.window || {};
      const all = k.all_time || {};
      return {
        keyId: k.key_id || '',
        keyName: k.key_name || k.key_id || '未命名密钥',
        requests: Number(win.requests) || 0,
        errors: Number(win.errors) || 0,
        totalTokens: Number(win.total_tokens) || 0,
        credit: Number(win.credit) || 0,
        cacheHitPct: win.cache_hit_pct == null ? null : Number(win.cache_hit_pct),
        ttftAvg: Number(win.ttft_ms_avg) || null,
        allTokens: Number(all.total_tokens) || 0,
      };
    })
    .filter((k) => k.requests > 0 || k.totalTokens > 0)
    .sort((a, b) => b.totalTokens - a.totalTokens || b.requests - a.requests || compareText(a.keyName, b.keyName));
  const shares = allocateShares(rows.map((k) => k.totalTokens));
  rows.forEach((k, i) => {
    k.share = shares[i];
  });
  return rows;
}

/** 这把密钥在窗口内没有调用，但历史上有：表格下沿提示用。 */
export function idleKeyNames(data) {
  return ((data && data.keys) || [])
    .filter((k) => {
      const win = k.window || {};
      return !((Number(win.requests) || 0) > 0 || (Number(win.total_tokens) || 0) > 0);
    })
    .map((k) => k.key_name || k.key_id || '未命名密钥')
    .sort((a, b) => compareText(a, b));
}

function accountOptionLabel(uid, info) {
  const name = (info && info.nickname) || String(uid || '').slice(0, 8);
  return name + ' · ' + String(uid || '').slice(0, 6);
}

/** 矩阵两个筛选下拉的选项，全部从本次数据里推出来：筛不出结果的选项不如不给。 */
export function matrixOptions(usage) {
  const names = (usage && usage.accounts_map) || {};
  const seen = new Set();
  Object.values((usage && usage.by_model_acct) || {}).forEach((byRealm) => {
    Object.values(byRealm || {}).forEach((byAcct) => {
      Object.keys(byAcct || {}).forEach((uid) => {
        if (uid && uid !== '(unattributed)') seen.add(uid);
      });
    });
  });
  const accounts = [...seen]
    .sort((a, b) => {
      const na = (names[a] || {}).nickname || a;
      const nb = (names[b] || {}).nickname || b;
      return compareText(na, nb) || compareText(a, b);
    })
    .map((uid) => ({ uid, label: accountOptionLabel(uid, names[uid]) }));
  const byModel = (usage && usage.by_model) || {};
  const models = Object.keys(byModel)
    .filter((id) => !id.endsWith('-model') && (Number(byModel[id].requests) || 0) > 0)
    .sort((a, b) => compareText(a, b));
  return { accounts, models };
}

/** 数据换了范围后，仍然存在的筛选保留，找不到的筛选项清空。 */
export function reconcileFilters(filters, options) {
  const account = filters.account && options.accounts.some((a) => a.uid === filters.account) ? filters.account : '';
  const model = filters.model && options.models.includes(filters.model) ? filters.model : '';
  return { account, model };
}

function emptyStat() {
  return { requests: 0, prompt_tokens: 0, completion_tokens: 0, reasoning_tokens: 0, cached_tokens: 0, total_tokens: 0 };
}

/** 一行明细的性能桶：先取这一账号在这一出口的，再取该出口的，最后退回该模型的整体值。 */
function perfBucket(perf, id, realm, acct) {
  if (!perf) return {};
  const byRealm = (perf.by_model_realm || {})[id] || {};
  const byAcct = (perf.by_model_acct || {})[id] || {};
  const realmBucket = realm ? byRealm[realm] || null : null;
  const acctBucket = realm && acct && byAcct[realm] ? byAcct[realm][acct] || null : null;
  return acctBucket || realmBucket || (perf.by_model || {})[id] || {};
}

function weighted(bucket, field, acc) {
  const stat = bucket && bucket[field];
  if (stat && stat.samples) {
    acc.weight += stat.avg * stat.samples;
    acc.samples += stat.samples;
  }
}

/**
 * 模型性能与用量矩阵。
 *
 * 一个模型在一个出口上占一行；同一出口下有多个账号时按账号分行，这样区域、账号与
 * 右边那一串数字说的是同一件事，不会把两个账号的消耗合成一个看不清来源的数。
 * 合计行在筛选生效时只用可见行重算，延迟与速度按各自样本数加权。
 */
export function buildMatrix(usage, perf, filters = {}) {
  const modelFilter = filters.model || '';
  const accountFilter = filters.account || '';
  const u = usage || {};
  const bmPerf = (perf && perf.by_model) || {};
  const modelMap = {};
  Object.entries(u.by_model || {}).forEach(([id, m]) => {
    if ((Number(m.requests) || 0) > 0 && !id.endsWith('-model')) modelMap[id] = Object.assign({ id }, m);
  });
  Object.entries(bmPerf).forEach(([id, p]) => {
    if ((Number(p.requests) || 0) > 0 && !id.endsWith('-model') && !modelMap[id]) {
      modelMap[id] = Object.assign(emptyStat(), { id, requests: Number(p.requests) || 0 });
    }
  });

  const realmMap = u.by_model_realm || {};
  const acctMap = u.by_model_acct || {};
  const perfAcctMap = (perf && perf.by_model_acct) || {};
  const rows = [];
  Object.values(modelMap)
    .sort((a, b) => {
      const ta = Number(a.total_tokens) || Number(a.requests) || 0;
      const tb = Number(b.total_tokens) || Number(b.requests) || 0;
      return tb - ta || compareText(a.id, b.id);
    })
    .forEach((m) => {
      if (modelFilter && m.id !== modelFilter) return;
      const per = realmMap[m.id];
      let realms = per ? Object.keys(per).filter((r) => (Number(per[r].requests) || 0) > 0) : [];
      if (realms.length > 1) {
        realms = realms.slice().sort((x, y) => (Number(per[y].total_tokens) || 0) - (Number(per[x].total_tokens) || 0));
      }
      const parts = [];
      (realms.length ? realms : ['']).forEach((r) => {
        const accts = (acctMap[m.id] || {})[r] || {};
        const perfAccts = (perfAcctMap[m.id] || {})[r] || {};
        // 两张表的账号取并集：只失败过的账号只出现在性能表里，丢掉它就藏起了失败。
        const seen = [...new Set(Object.keys(accts).concat(Object.keys(perfAccts)))].filter((uid) => {
          const a = accts[uid] || {};
          const p = perfAccts[uid] || {};
          return (a.requests || 0) > 0 || (a.errors || 0) > 0
            || (p.requests || 0) > 0 || (p.errors || 0) > 0 || (p.client_aborted || 0) > 0;
        });
        const shown = accountFilter ? seen.filter((uid) => uid === accountFilter) : seen;
        if (shown.length) {
          shown.sort((x, y) => (Number(accts[y] && accts[y].total_tokens) || 0) - (Number(accts[x] && accts[x].total_tokens) || 0)
            || compareText(x, y));
          shown.forEach((uid) => {
            const stat = accts[uid] || emptyStat();
            parts.push({ stat, realm: r, acct: uid, accounts: { [uid]: Number(stat.requests) || 0 } });
          });
        } else if (!accountFilter) {
          // 这份数据没有按账号分开统计：改用该出口的整体值，整行继续显示。
          const fallback = (per && per[r]) || m;
          parts.push({ stat: fallback, realm: r, acct: '', accounts: fallback.accounts || {} });
        }
      });
      parts.forEach((part, i) => {
        const stat = part.stat || {};
        const bucket = perfBucket(perf, m.id, part.realm, part.acct);
        const promptTokens = Number(stat.prompt_tokens) || 0;
        const cachedTokens = Number(stat.cached_tokens) || 0;
        rows.push({
          id: m.id,
          realm: part.realm,
          acct: part.acct,
          accounts: part.accounts,
          split: parts.length > 1,
          splitAccounts: parts.length,
          first: i === 0,
          requests: Number(stat.requests) || 0,
          errors: Number(bucket.errors) || 0,
          totalTokens: Number(stat.total_tokens) || 0,
          promptTokens,
          completionTokens: Number(stat.completion_tokens) || 0,
          reasoningTokens: Number(stat.reasoning_tokens) || 0,
          cachedTokens,
          ttftAvg: bucket.ttft_ms ? bucket.ttft_ms.avg : null,
          ttftP50: bucket.ttft_ms ? bucket.ttft_ms.p50 : null,
          ttftP90: bucket.ttft_ms ? bucket.ttft_ms.p90 : null,
          ttftP95: bucket.ttft_ms ? bucket.ttft_ms.p95 : null,
          ttftP99: bucket.ttft_ms ? bucket.ttft_ms.p99 : null,
          tps: bucket.tokens_per_sec ? bucket.tokens_per_sec.avg : null,
          wallMs: bucket.wall_ms ? bucket.wall_ms.avg : null,
          cachePct: promptTokens > 0 ? (cachedTokens / promptTokens) * 100
            : (bucket.cache_hit_pct ? bucket.cache_hit_pct.avg : null),
        });
      });
    });

  const filtering = !!(modelFilter || accountFilter);
  const summary = { filtering };
  if (!filtering) {
    summary.label = '全部模型合计';
    summary.accountsLabel = '全部账号';
    summary.requests = Number(u.requests) || 0;
    summary.errors = Number(perf && perf.errors) || 0;
    summary.totalTokens = Number(u.total_tokens) || 0;
    summary.promptTokens = Number(u.prompt_tokens) || 0;
    summary.completionTokens = Number(u.completion_tokens) || 0;
    summary.reasoningTokens = Number(u.reasoning_tokens) || 0;
    summary.ttftAvg = perf && perf.ttft_ms ? perf.ttft_ms.avg : null;
    summary.ttftP50 = perf && perf.ttft_ms ? perf.ttft_ms.p50 : null;
    summary.ttftP90 = perf && perf.ttft_ms ? perf.ttft_ms.p90 : null;
    summary.ttftP95 = perf && perf.ttft_ms ? perf.ttft_ms.p95 : null;
    summary.ttftP99 = perf && perf.ttft_ms ? perf.ttft_ms.p99 : null;
    summary.tps = perf && perf.tokens_per_sec ? perf.tokens_per_sec.avg : null;
    summary.wallMs = perf && perf.wall_ms ? perf.wall_ms.avg : null;
    summary.cachePct = perf && perf.cache_hit_pct ? perf.cache_hit_pct.avg : null;
  } else {
    const acc = {
      requests: 0, errors: 0, totalTokens: 0, promptTokens: 0, completionTokens: 0, reasoningTokens: 0,
      ttft: { weight: 0, samples: 0 }, tps: { weight: 0, samples: 0 },
      wall: { weight: 0, samples: 0 }, cache: { weight: 0, samples: 0 },
    };
    rows.forEach((row) => {
      acc.requests += row.requests;
      acc.totalTokens += row.totalTokens;
      acc.promptTokens += row.promptTokens;
      acc.completionTokens += row.completionTokens;
      acc.reasoningTokens += row.reasoningTokens;
      const bucket = perfBucket(perf, row.id, row.realm, row.acct);
      acc.errors += Number(bucket.errors) || 0;
      weighted(bucket, 'ttft_ms', acc.ttft);
      weighted(bucket, 'tokens_per_sec', acc.tps);
      weighted(bucket, 'wall_ms', acc.wall);
      weighted(bucket, 'cache_hit_pct', acc.cache);
    });
    summary.label = '筛选结果合计';
    summary.accountsLabel = '已筛选';
    summary.requests = acc.requests;
    summary.errors = acc.errors;
    summary.totalTokens = acc.totalTokens;
    summary.promptTokens = acc.promptTokens;
    summary.completionTokens = acc.completionTokens;
    summary.reasoningTokens = acc.reasoningTokens;
    summary.ttftAvg = acc.ttft.samples ? acc.ttft.weight / acc.ttft.samples : null;
    // 筛选后的合计只报加权平均值，没有可用的分位数样本，页面对空值不画 P50、P90、P95 与 P99。
    summary.ttftP50 = null;
    summary.ttftP90 = null;
    summary.ttftP95 = null;
    summary.ttftP99 = null;
    summary.tps = acc.tps.samples ? acc.tps.weight / acc.tps.samples : null;
    summary.wallMs = acc.wall.samples ? acc.wall.weight / acc.wall.samples : null;
    summary.cachePct = acc.cache.samples ? acc.cache.weight / acc.cache.samples : null;
  }

  // 行的占比按表内合计算，零头用最大余数法分配，各行的百分比加起来正好 100。
  const shares = allocateShares(rows.map((row) => row.totalTokens));
  rows.forEach((row, i) => {
    row.share = shares[i];
  });
  return { rows, summary, modelCount: new Set(rows.map((r) => r.id)).size };
}
