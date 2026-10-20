// 用量页：按时间范围统计请求量、Token 消耗、积分消耗与模型性能。

import { html, render, nothing, ref, createRef } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { fmtNumber, fmtCompact, fmtCredit, fmtPercent, fmtLatency, fmtDateTime, toLocalInputValue, fromLocalInputValue } from '../core/format.js';
import { pageHeader, statCard, segmented, chip, realmBadge, emptyState, loadErrorPage, loadErrorInline, skeletonStats, skeletonBlock } from '../ui/widgets.js';
import { createTrendChart, ensureChart } from '../ui/chart.js';
import { toastError } from '../ui/toast.js';
import {
  RANGES, rangeLabel, scopeRealm, scopeText, buildQuery, trendGranularity, TREND_MODES, trendSeries,
  accountRows, keyRows, idleKeyNames, matrixOptions, reconcileFilters, buildMatrix,
} from './stats/logic.js';

export const autoRefresh = true;

const STATS = 'stats';

export function mount(host) {
  const chartCanvas = createRef();
  let chart = null;
  let chartCanvasEl = null; // 建图时用的画布节点，用于识别节点被重建的情况
  let chartPending = false;
  let chartErrorShown = false;
  let range = 'today';
  let trendMode = 'both';
  let sinceInput = '';
  let untilInput = '';
  let includeAll = false;
  let filters = { account: '', model: '' };
  let analytics = null;
  let usage = null;
  let perf = null;
  let trend = null;
  let month = null;
  let missing = [];
  let fatal = '';
  let phase = 'loading';
  let seq = 0;

  function draw() {
    render(view(), host);
    syncChart().catch((err) => {
      if (chartErrorShown) return;
      chartErrorShown = true;
      toastError('趋势图渲染失败', err);
    });
  }

  /** 任何一项取数失败都不清空已有内容：下游按 null 渲染破折号或空态。 */
  function apply(key, value) {
    if (key === 'analytics') analytics = value;
    else if (key === 'usage') usage = value;
    else if (key === 'perf') perf = value;
    else if (key === 'trend') trend = value;
    else if (key === 'month') month = value;
  }

  function query() {
    return buildQuery({
      realm: scopeRealm(getState().viewRealm, includeAll),
      range,
      since: fromLocalInputValue(sinceInput),
      until: fromLocalInputValue(untilInput),
    });
  }

  async function load() {
    const mine = ++seq;
    const suffix = query();
    const jobs = [
      { key: 'analytics', label: '用量总览', url: '/usage/analytics' + suffix },
      { key: 'usage', label: '账号与模型用量', url: '/usage' + suffix },
      { key: 'perf', label: '性能指标', url: '/usage/perf' + suffix },
      { key: 'trend', label: '调用趋势', url: '/usage/trend' + suffix },
      {
        key: 'month',
        label: '本月积分',
        url: '/usage/month-credit?realm=' + encodeURIComponent(scopeRealm(getState().viewRealm, includeAll)),
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

  async function syncChart() {
    const canvas = chartCanvas.value;
    if (!canvas) return;
    // 首屏失败视图上没有画布；恢复后 lit-html 会新建一个节点，旧的图表必须重建。
    if (chart && chartCanvasEl !== canvas) {
      chart.destroy();
      chart = null;
    }
    const points = chartPoints();
    if (!points) {
      if (chart) { chart.destroy(); chart = null; }
      return;
    }
    if (chart) {
      chart.update(points);
      return;
    }
    if (chartPending) return;
    chartPending = true;
    try {
      await ensureChart();
    } catch (err) {
      if (!chartErrorShown) {
        chartErrorShown = true;
        toastError('图表库加载失败', err);
      }
      return;
    } finally {
      chartPending = false;
    }
    // 等加载期间页面可能已经卸载，或数据又换了一轮；用当前节点与当前数据建图。
    const current = chartCanvas.value;
    if (!current || !current.isConnected) return;
    const latest = chartPoints();
    if (!latest) return;
    chart = createTrendChart(current, latest);
    chartCanvasEl = current;
  }

  function selectTrendMode(id) {
    if (trendMode === id) return;
    trendMode = id;
    draw();
  }

  function selectRange(id) {
    if (range === id) return;
    range = id;
    if (id === 'custom') prefillCustomRange();
    draw();
    load();
  }

  /** 自定义区间第一次打开时把起点填成今天零点：空输入等于不限，画面会和「全部」一模一样，看起来像按钮坏了。终点留空表示到现在。 */
  function prefillCustomRange() {
    if (sinceInput) return;
    const d = new Date();
    sinceInput = toLocalInputValue(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000);
  }

  function toggleScope() {
    includeAll = !includeAll;
    draw();
    load();
  }

  function setFilter(patch) {
    filters = Object.assign({}, filters, patch);
    draw();
  }

  function numberOrDash(value) {
    return value == null || value === '' ? '—' : fmtNumber(value);
  }

  function tripleTokens(prompt, completion, reasoning) {
    return html`
      <span class="text-2xs nums">
        <span class="tone-info">输入 ${fmtNumber(prompt)}</span>
        <span class="muted"> · </span>
        <span class="tone-success">输出 ${fmtNumber(completion)}</span>
        <span class="muted"> · </span>
        <span class="tone-violet">思考 ${fmtNumber(reasoning)}</span>
      </span>`;
  }

  function statCards() {
    const win = analytics ? (analytics.summary || {}).window || {} : null;
    const all = analytics ? (analytics.summary || {}).all_time || {} : null;
    const requestValue = win ? numberOrDash(win.requests) : '—';
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
        ${statCard({ label: rangeLabel(range) + ' 请求数', value: requestValue, icon: 'Activity', hint: requestHint, delay: 0 })}
        ${statCard({ label: rangeLabel(range) + ' Token 消耗', value: win ? fmtCompact(win.total_tokens || 0) : '—', icon: 'Zap', hint: tokenHint, delay: 0.05 })}
        ${statCard({ label: rangeLabel(range) + ' 积分消耗', value: win ? fmtCredit(win.credit || 0) : '—', icon: 'Coins', hint: creditHint, delay: 0.1 })}
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
          <span class="card-sub">${buckets ? trendGranularity(range) : '趋势数据未读取到，稍后自动重试'}</span>
          ${buckets ? segmented(TREND_MODES, trendMode, selectTrendMode, 'seg-sm') : nothing}
        </div>
        <div class="stats-chart">
          <canvas ${ref(chartCanvas)}></canvas>
          ${buckets && !buckets.length ? html`<div class="chart-empty text-xs muted">${rangeLabel(range)}没有调用记录</div>` : nothing}
        </div>
      </div>`;
  }

  function accountTable() {
    const rows = analytics ? accountRows(analytics) : [];
    return html`
      <div class="card card-flush">
        <div class="card-head">
          <h2 class="card-title">各账号用量与模型消耗分布</h2>
          <span class="card-sub">${analytics ? (rows.length ? rows.length + ' 个账号' : '暂无数据') : '未读取到'}</span>
        </div>
        ${rows.length === 0
          ? html`<div class="empty text-xs muted">${rangeLabel(range)}没有账号用量数据</div>`
          : html`
            <div class="table-wrap">
              <table class="table">
                <thead>
                  <tr>
                    <th>账号</th>
                    <th>所属区域</th>
                    <th class="right">剩余积分</th>
                    <th class="right">请求次数</th>
                    <th class="right">消耗 Token</th>
                    <th class="right">消耗积分</th>
                    <th>调用的模型分布</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map((row) => html`
                    <tr>
                      <td>
                        <div class="medium truncate">${row.nickname}</div>
                        <div class="cell-mono">${row.uid.slice(0, 10)}</div>
                      </td>
                      <td>${row.realm ? realmBadge(row.realm) : nothing}</td>
                      <td class="right nums">${row.credits && row.credits.remain != null
                        ? html`<b>${fmtNumber(row.credits.remain)}</b>${row.credits.size ? html`<span class="muted text-2xs"> / ${fmtNumber(row.credits.size)}</span>` : nothing}`
                        : html`<span class="muted">—</span>`}</td>
                      <td class="right nums">${fmtNumber(row.requests)}</td>
                      <td class="right nums"><b>${fmtNumber(row.totalTokens)}</b></td>
                      <td class="right nums">${Number(row.credit).toFixed(2)}</td>
                      <td>
                        ${row.models.length === 0
                          ? html`<span class="muted text-2xs">无调用</span>`
                          : html`<div class="model-pills">${row.models.map((m) => html`
                              <span class="model-pill"><span class="cell-mono">${m.id}</span> ${fmtNumber(m.requests)} 次 · ${fmtNumber(m.tokens)} token</span>`)}</div>`}
                      </td>
                    </tr>`)}
                </tbody>
              </table>
            </div>`}
      </div>`;
  }

  function keyTable() {
    const rows = analytics ? keyRows(analytics) : [];
    const idle = analytics ? idleKeyNames(analytics) : [];
    return html`
      <div class="card card-flush">
        <div class="card-head">
          <h2 class="card-title">${icon('KeyRound')}按密钥</h2>
          <span class="card-sub">每把密钥在当前范围内的调用与消耗，密钥名取自密钥列表</span>
        </div>
        ${rows.length === 0
          ? html`<div class="empty text-xs muted">${rangeLabel(range)}没有密钥调用数据</div>`
          : html`
            <div class="table-wrap">
              <table class="table">
                <thead>
                  <tr>
                    <th>密钥</th>
                    <th class="right">请求次数</th>
                    <th class="right">失败</th>
                    <th class="right">消耗 Token</th>
                    <th class="right">消耗积分</th>
                    <th class="right">缓存命中率</th>
                    <th class="right">首字延迟</th>
                    <th>用量占比</th>
                  </tr>
                </thead>
                <tbody>
                  ${rows.map((row) => html`
                    <tr>
                      <td><div class="medium truncate">${row.keyName}</div><div class="cell-mono">${row.keyId}</div></td>
                      <td class="right nums">${fmtNumber(row.requests)}</td>
                      <td class="right nums">${row.errors ? html`<span class="tone-danger">${fmtNumber(row.errors)}</span>` : html`<span class="muted">0</span>`}</td>
                      <td class="right nums"><b>${fmtNumber(row.totalTokens)}</b></td>
                      <td class="right nums">${Number(row.credit).toFixed(2)}</td>
                      <td class="right nums">${row.cacheHitPct == null ? '—' : fmtPercent(row.cacheHitPct)}</td>
                      <td class="right nums">${row.ttftAvg ? fmtLatency(row.ttftAvg) : '—'}</td>
                      <td class="share-cell">
                        <span class="share-track"><span class="share-bar" style="width:${Math.max(3, row.share)}%"></span></span>
                        <span class="share-pct text-2xs muted nums">${row.share}%</span>
                      </td>
                    </tr>`)}
                </tbody>
              </table>
            </div>`}
        ${idle.length
          ? html`<div class="card-sub" style="padding:10px 16px 14px">另有 ${idle.length} 把密钥在这个范围没有调用：${idle.slice(0, 6).join('、')}${idle.length > 6 ? ' 等' : ''}。</div>`
          : nothing}
      </div>`;
  }

  function matrixCell(row) {
    const names = (usage && usage.accounts_map) || {};
    if (row.acct) {
      const name = (names[row.acct] || {}).nickname || row.acct.slice(0, 8);
      return html`<b>${name}</b>`;
    }
    const mapped = Object.entries(row.accounts || {}).filter(([uid]) => uid && uid !== '(unattributed)');
    if (!mapped.length) return html`<span class="muted">全部账号</span>`;
    return html`<div class="model-pills">${mapped.map(([uid, count]) => html`
      <span class="badge badge-sm">${(names[uid] || {}).nickname || uid.slice(0, 6)} <span class="muted">${count}</span></span>`)}</div>`;
  }

  function matrixTable() {
    const options = matrixOptions(usage);
    const built = usage ? buildMatrix(usage, perf, filters) : null;
    const rows = built ? built.rows : [];
    const head = html`
      <div class="card-head">
        <h2 class="card-title">${icon('Cpu')}模型性能指标与用量一览</h2>
        <div class="row-wrap">
          <span class="card-sub">${built ? (built.modelCount ? built.modelCount + ' 个模型，共 ' + rows.length + ' 行明细' : '暂无模型请求') : '未读取到'}</span>
          <label class="row text-2xs muted">账号
            <select class="select input-sm" @change=${(e) => setFilter({ account: e.target.value })}>
              <option value="" ?selected=${filters.account === ''}>全部账号</option>
              ${options.accounts.map((opt) => html`<option value=${opt.uid} ?selected=${filters.account === opt.uid}>${opt.label}</option>`)}
            </select>
          </label>
          <label class="row text-2xs muted">模型
            <select class="select input-sm" @change=${(e) => setFilter({ model: e.target.value })}>
              <option value="" ?selected=${filters.model === ''}>全部模型</option>
              ${options.models.map((id) => html`<option value=${id} ?selected=${filters.model === id}>${id}</option>`)}
            </select>
          </label>
        </div>
      </div>`;
    if (!built) return html`<div class="card">${head}<div class="empty text-xs muted">用量数据没有读取到，稍后自动重试</div></div>`;
    if (!rows.length) {
      return html`<div class="card">${head}${emptyState({ icon: 'Cpu', title: '暂无模型请求', desc: '当前范围与筛选条件下没有任何模型调用记录。' })}</div>`;
    }
    const summary = built.summary;
    return html`
      <div class="card card-flush">
        ${head}
        <div class="table-wrap">
          <table class="table">
            <thead>
              <tr>
                <th>模型名称</th>
                <th class="center">区域</th>
                <th>账号</th>
                <th class="right">请求数</th>
                <th class="right">失败</th>
                <th class="right">总 Token</th>
                <th class="matrix-opt">输入 / 输出 / 思考</th>
                <th class="right">首字延迟</th>
                <th class="right">生成速度</th>
                <th class="right matrix-opt">端到端耗时</th>
                <th class="right">缓存命中率</th>
                <th>用量占比</th>
              </tr>
            </thead>
            <tbody>
              <tr class="matrix-summary">
                <td><b>${summary.label}</b></td>
                <td class="center"><span class="muted">—</span></td>
                <td><span class="muted">${summary.accountsLabel}</span></td>
                <td class="right nums">${fmtNumber(summary.requests)}</td>
                <td class="right nums">${summary.errors ? html`<span class="tone-danger">${fmtNumber(summary.errors)}</span>` : html`<span class="muted">0</span>`}</td>
                <td class="right nums"><b>${fmtNumber(summary.totalTokens)}</b></td>
                <td class="matrix-opt">${tripleTokens(summary.promptTokens, summary.completionTokens, summary.reasoningTokens)}</td>
                <td class="right nums">${summary.ttftAvg ? html`
                  <div>${fmtLatency(summary.ttftAvg)}</div>
                  ${summary.ttftP50 != null ? html`<div class="muted text-2xs">P50 ${fmtLatency(summary.ttftP50)}</div>` : nothing}
                  ${summary.ttftP90 != null ? html`<div class="muted text-2xs">P90 ${fmtLatency(summary.ttftP90)}</div>` : nothing}
                  ${summary.ttftP95 != null ? html`<div class="muted text-2xs">P95 ${fmtLatency(summary.ttftP95)}</div>` : nothing}
                  ${summary.ttftP99 != null ? html`<div class="muted text-2xs">P99 ${fmtLatency(summary.ttftP99)}</div>` : nothing}` : '—'}</td>
                <td class="right nums">${summary.tps ? Number(summary.tps).toFixed(1) + ' tok/s' : '—'}</td>
                <td class="right nums matrix-opt">${summary.wallMs ? fmtLatency(summary.wallMs) : '—'}</td>
                <td class="right nums">${summary.cachePct == null ? '—' : fmtPercent(summary.cachePct)}</td>
                <td class="share-cell"><span class="share-track"><span class="share-bar" style="width:100%"></span></span> <span class="share-pct text-2xs muted nums">100%</span></td>
              </tr>
              ${rows.map((row) => html`
                <tr>
                  <td>
                    <div class="cell-mono">${row.id}</div>
                    ${row.split && row.first ? html`<div class="text-2xs muted nowrap">共 ${row.splitAccounts} 个账号参与此模型处理</div>` : nothing}
                  </td>
                  <td class="center">${row.realm ? realmBadge(row.realm) : html`<span class="muted">—</span>`}</td>
                  <td>${matrixCell(row)}</td>
                  <td class="right nums">${fmtNumber(row.requests)}</td>
                  <td class="right nums">${row.errors ? html`<span class="tone-danger">${fmtNumber(row.errors)}</span>` : html`<span class="muted">0</span>`}</td>
                  <td class="right nums"><b>${fmtNumber(row.totalTokens)}</b></td>
                  <td class="matrix-opt">${tripleTokens(row.promptTokens, row.completionTokens, row.reasoningTokens)}</td>
                  <td class="right nums">${row.ttftAvg ? html`
                    <div>${fmtLatency(row.ttftAvg)}</div>
                    ${row.ttftP50 != null ? html`<div class="muted text-2xs">P50 ${fmtLatency(row.ttftP50)}</div>` : nothing}
                    ${row.ttftP90 != null ? html`<div class="muted text-2xs">P90 ${fmtLatency(row.ttftP90)}</div>` : nothing}
                    ${row.ttftP95 != null ? html`<div class="muted text-2xs">P95 ${fmtLatency(row.ttftP95)}</div>` : nothing}
                    ${row.ttftP99 != null ? html`<div class="muted text-2xs">P99 ${fmtLatency(row.ttftP99)}</div>` : nothing}` : '—'}</td>
                  <td class="right nums">${row.tps ? Number(row.tps).toFixed(1) + ' tok/s' : '—'}</td>
                  <td class="right nums matrix-opt">${row.wallMs ? fmtLatency(row.wallMs) : '—'}</td>
                  <td class="right nums">${row.cachePct == null ? '—' : fmtPercent(row.cachePct)}</td>
                  <td class="share-cell">
                    <span class="share-track"><span class="share-bar" style="width:${Math.max(3, row.share)}%"></span></span>
                    <span class="share-pct text-2xs muted nums">${row.share}%</span>
                  </td>
                </tr>`)}
            </tbody>
          </table>
        </div>
        <div class="card-sub" style="padding:10px 16px 14px">
          首字延迟与生成速度取自最后一次性能采样，范围比采样更宽时这三列只覆盖较新的请求。
          ${perf && perf.sample_capped && perf.sample_from
            ? html`当前统计的是自 ${fmtDateTime(perf.sample_from)} 之后的部分请求。`
            : nothing}
        </div>
      </div>`;
  }

  function customRangeCard() {
    return html`
      <div class="card card-tight">
        <div class="row-wrap">
          <label class="row text-2xs muted">起点
            <input class="input input-sm" type="datetime-local" .value=${sinceInput}
                   @change=${(e) => { sinceInput = e.target.value; load(); }}>
          </label>
          <label class="row text-2xs muted">终点
            <input class="input input-sm" type="datetime-local" .value=${untilInput}
                   @change=${(e) => { untilInput = e.target.value; load(); }}>
          </label>
          <span class="text-2xs muted">终点留空表示统计到现在。</span>
        </div>
      </div>`;
  }

  function view() {
    const actions = html`
      ${segmented(RANGES, range, selectRange)}
      ${chip(includeAll ? '含两个版本' : '仅' + scopeText(false, getState().viewRealm), includeAll, toggleScope)}`;
    const desc = '按时间范围统计请求量、Token 消耗、积分消耗与模型性能。当前统计：' + scopeText(includeAll, getState().viewRealm) + '。';
    if (phase === 'loading') {
      return html`
        <div class="page">
          ${pageHeader({ path: STATS, title: '用量', desc, actions })}
          ${skeletonStats(4)}
          ${skeletonBlock(300)}
          ${skeletonBlock(260)}
        </div>`;
    }
    if (phase === 'error') {
      return html`<div class="page">${pageHeader({ path: STATS, title: '用量', desc, actions })}${loadErrorPage(fatal || '用量数据没有读取到', retry)}</div>`;
    }
    return html`
      <div class="page">
        ${pageHeader({ path: STATS, title: '用量', desc, actions })}
        ${range === 'custom' ? customRangeCard() : nothing}
        ${missing.length
          ? loadErrorInline('这些数据没有刷新成功：' + missing.join('、') + '。下面显示的是上一次读取到的结果。', retry)
          : nothing}
        ${statCards()}
        ${trendCard()}
        ${accountTable()}
        ${keyTable()}
        ${matrixTable()}
      </div>`;
  }

  draw();
  load();

  return {
    refresh: () => load(),
    unmount() {
      seq += 1;
      if (chart) { chart.destroy(); chart = null; }
    },
  };
}
