// 用量页：按时间范围统计请求量、Token 消耗、积分消耗与模型性能。
// 时间范围控件在 stats/range.js，表格模板在 stats/tables.js，统计口径在 stats/logic.js。

import { html, render, nothing, ref, createRef } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { fmtNumber, fmtCompact, fmtCredit, fmtNumberDash, fmtPercent, fmtLatency } from '../core/format.js';
import { pageHeader, statCard, segmented, loadErrorPage, loadErrorInline, skeletonStats, skeletonBlock } from '../ui/widgets.js';
import { createChartHost } from '../ui/chart.js';
import {
  rangeLabel, scopeText, trendGranularity, TREND_MODES, trendSeries,
  accountRows, keyRows, idleKeyNames, matrixOptions, reconcileFilters,
} from './stats/logic.js';
import { tripleTokens, accountTable, keyTable, matrixTable } from './stats/tables.js';
import { createRangeControls } from './stats/range.js';

export const autoRefresh = true;

const STATS = 'stats';

export function mount(host) {
  const chartCanvas = createRef();
  const chartHost = createChartHost(chartCanvas, chartPoints);
  let trendMode = 'both';
  let analytics = null;
  let usage = null;
  let perf = null;
  let trend = null;
  let month = null;
  let missing = [];
  let fatal = '';
  let phase = 'loading';
  let seq = 0;
  let filters = { account: '', model: '' };
  const rangeCtl = createRangeControls({ redraw: draw, reload: load });

  function draw() {
    render(view(), host);
    chartHost.sync();
  }

  /** 任何一项取数失败都不清空已有内容：下游按 null 渲染破折号或空态。 */
  function apply(key, value) {
    if (key === 'analytics') analytics = value;
    else if (key === 'usage') usage = value;
    else if (key === 'perf') perf = value;
    else if (key === 'trend') trend = value;
    else if (key === 'month') month = value;
  }

  async function load() {
    const mine = ++seq;
    const suffix = rangeCtl.query();
    const jobs = [
      { key: 'analytics', label: '用量总览', url: '/usage/analytics' + suffix },
      { key: 'usage', label: '账号与模型用量', url: '/usage' + suffix },
      { key: 'perf', label: '性能指标', url: '/usage/perf' + suffix },
      { key: 'trend', label: '调用趋势', url: '/usage/trend' + suffix },
      {
        key: 'month',
        label: '本月积分',
        url: '/usage/month-credit?realm=' + encodeURIComponent(rangeCtl.realm()),
      },
    ];
    const settled = await Promise.allSettled(jobs.map((job) => getJSON(job.url)));
    if (mine !== seq) return;
    const failed = [];
    settled.forEach((result, i) => {
      if (result.status === 'fulfilled') apply(jobs[i].key, result.value);
      else failed.push(jobs[i].label);
    });
    missing = failed;
    if (analytics === null) {
      fatal = '用量数据没有读取到，这一页的统计无法计算';
      phase = 'error';
    } else {
      phase = 'ready';
      // 数据变了以后，找不到的筛选项就地清掉，避免筛选出一张永远空白的表。
      // 用量明细这一份没取到时不清理：那一份的失败不代表筛选项已经消失。
      if (usage) {
        const options = matrixOptions(usage);
        filters = reconcileFilters(filters, options);
      }
    }
    draw();
  }

  function retry() {
    phase = 'loading';
    fatal = '';
    draw();
    return load();
  }

  function chartPoints() {
    if (!trend) return null;
    return trendSeries(trendMode, trend.buckets || [], fmtNumber, fmtCompact, { errors: true });
  }

  function selectTrendMode(id) {
    if (trendMode === id) return;
    trendMode = id;
    draw();
  }

  function setFilter(patch) {
    filters = Object.assign({}, filters, patch);
    draw();
  }

  function statCards() {
    const win = analytics ? (analytics.summary || {}).window || {} : null;
    const all = analytics ? (analytics.summary || {}).all_time || {} : null;
    const requestValue = win ? fmtNumberDash(win.requests) : '—';
    const total = win ? (Number(win.requests) || 0) + (Number(win.errors) || 0) : 0;
    const success = total > 0 ? ((Number(win.requests) || 0) / total) * 100 : 100;
    const requestHint = win
      ? html`${(Number(win.errors) || 0) > 0 ? '失败 ' + fmtNumber(win.errors) + ' 次' : '全部成功'} · 成功率 ${fmtPercent(success)}<br>全部 ${fmtNumber(all.requests || 0)} 次`
      : '用量总览未读取到';
    const tokenHint = win
      ? html`${tripleTokens(win.prompt_tokens, win.completion_tokens, win.reasoning_tokens)}<br>全部 ${fmtCompact(all.total_tokens || 0)}`
      : '用量总览未读取到';
    const creditHint = win
      ? html`全部 ${fmtCredit(all.credit || 0)} 积分<br>平均每次请求 ${fmtCredit((Number(win.credit) || 0) / Math.max(1, Number(win.requests) || 0))} 积分`
      : '用量总览未读取到';
    const cacheHint = win
      ? html`平均首字延迟 ${win.ttft_ms_avg ? fmtLatency(win.ttft_ms_avg) : '—'} · 生成速度 ${win.speed_avg ? Number(win.speed_avg).toFixed(1) + ' tok/s' : '—'}<br>缓存命中率 ${fmtPercent(all.cache_hit_pct || 0)}`
      : '用量总览未读取到';
    return html`
      <div class="grid-stats cols-4">
        ${statCard({ label: rangeLabel(rangeCtl.range) + ' 请求数', value: requestValue, icon: 'Activity', hint: requestHint, delay: 0 })}
        ${statCard({ label: rangeLabel(rangeCtl.range) + ' Token 消耗', value: win ? fmtCompact(win.total_tokens || 0) : '—', icon: 'Zap', hint: tokenHint, delay: 0.05 })}
        ${statCard({ label: rangeLabel(rangeCtl.range) + ' 积分消耗', value: win ? fmtCredit(win.credit || 0) : '—', icon: 'Coins', hint: creditHint, delay: 0.1 })}
        ${statCard({ label: '缓存命中率', value: win ? fmtPercent(win.cache_hit_pct || 0) : '—', icon: 'Database', tone: 'info', hint: cacheHint, delay: 0.15 })}
      </div>
      ${month
        ? (month.ok === false
          ? html`<div class="text-xs tone-danger">本月消耗积分读取失败</div>`
          : html`<div class="text-xs muted">本月消耗了积分 ${fmtCredit(month.credit || 0)}</div>`)
        : nothing}`;
  }

  function trendCard() {
    const buckets = trend ? trend.buckets || [] : null;
    return html`
      <div class="card">
        <div class="card-head">
          <h2 class="card-title">${icon('TrendingUp')}请求量与 Token 消耗趋势</h2>
          <span class="card-sub">${buckets ? trendGranularity(rangeCtl.range) : '趋势数据未读取到，稍后自动重试'}</span>
          ${buckets ? segmented(TREND_MODES, trendMode, selectTrendMode, 'seg-sm') : nothing}
        </div>
        <div class="stats-chart">
          <canvas ${ref(chartCanvas)}></canvas>
          ${buckets && !buckets.length ? html`<div class="chart-empty text-xs muted">${rangeLabel(rangeCtl.range)}没有调用记录</div>` : nothing}
        </div>
      </div>`;
  }

  function view() {
    const desc = '按时间范围统计请求量、Token 消耗、积分消耗与模型性能。当前统计：'
      + scopeText(rangeCtl.includeAll, getState().viewRealm) + '。';
    if (phase === 'loading') {
      return html`
        <div class="page">
          ${pageHeader({ path: STATS, title: '用量', desc, actions: rangeCtl.actions() })}
          ${skeletonStats(4)}
          ${skeletonBlock(300)}
          ${skeletonBlock(260)}
        </div>`;
    }
    if (phase === 'error') {
      return html`<div class="page">${pageHeader({ path: STATS, title: '用量', desc, actions: rangeCtl.actions() })}${loadErrorPage(fatal || '用量数据没有读取到', retry)}</div>`;
    }
    return html`
      <div class="page">
        ${pageHeader({ path: STATS, title: '用量', desc, actions: rangeCtl.actions() })}
        ${rangeCtl.range === 'custom' ? rangeCtl.customCard() : nothing}
        ${missing.length
          ? loadErrorInline('这些数据没有刷新成功：' + missing.join('、') + '。下面显示的是上一次读取到的结果。', retry)
          : nothing}
        ${statCards()}
        ${trendCard()}
        ${accountTable(analytics ? accountRows(analytics) : [], rangeCtl.range)}
        ${keyTable(analytics ? keyRows(analytics) : [], analytics ? idleKeyNames(analytics) : [], rangeCtl.range)}
        ${matrixTable({ usage, perf, filters, range: rangeCtl.range, setFilter })}
      </div>`;
  }

  draw();
  load();

  return {
    refresh: () => load(),
    unmount() {
      seq += 1;
      chartHost.destroy();
    },
  };
}
