// 仪表盘：当前查看版本的账号池健康度、网关出口与今日用量总览。

import { html, render, nothing, ref, createRef } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON } from '../core/api.js';
import { getState, subscribe, switchActiveRealm } from '../core/store.js';
import { fmtNumber, fmtCompact, fmtNumberDash, fmtRemain, realmLabel } from '../core/format.js';
import { hrefFor } from '../core/router.js';
import { pageHeader, statCard, badge, emptyState, loadErrorPage, loadErrorInline, skeletonStats, skeletonBlock, progressBar, copyButton, busyClick, segmented } from '../ui/widgets.js';
import { confirmDialog } from '../ui/dialog.js';
import { createChartHost } from '../ui/chart.js';
import { toastError, toast } from '../ui/toast.js';
import {
  realmAccounts, accountSummary, readyHint, creditsSummary, creditsHint, snapshotAccounts,
  availability, availabilityCounts, creditBar, expiryVisual, creditTone, keyEffect, usageHints, trendSummary,
} from './dashboard/logic.js';
import { TREND_MODES, trendSeries } from './stats/logic.js';

export const autoRefresh = true;

const SNAPSHOT_LIMIT = 9;
const DASH = 'dashboard';

export function mount(host) {
  const chartCanvas = createRef();
  const chartHost = createChartHost(chartCanvas, chartPoints);
  let accounts = null;     // GET /accounts?realm=all，按查看版本在前端筛选
  let usage = null;        // GET /usage?realm=<查看版本>&range=today
  let trend = null;        // GET /usage/trend?realm=<查看版本>&days=14
  let trendMode = 'both';  // 趋势图画什么：both 请求数与 Token、requests 只画请求数、tokens 只画 Token
  let keys = null;         // GET /settings 的 api_keys
  let scheduler = null;    // GET /scheduler
  let missing = [];        // 本次没有取到的数据名称
  let fatal = '';          // 首屏整体失败的原因
  let phase = 'loading';    // loading 首屏骨架、error 整页失败、ready 正常
  let seq = 0;

  function draw() {
    render(view(), host);
    chartHost.sync();
  }

  function apply(key, value) {
    if (key === 'accounts') accounts = value.accounts || [];
    else if (key === 'usage') usage = value;
    else if (key === 'trend') trend = value;
    else if (key === 'keys') keys = value.api_keys || [];
    else if (key === 'scheduler') scheduler = value;
  }

  async function load() {
    // 与用量页同一口径：晚发出的读取先作废旧一轮在途结果，避免旧数据盖掉新数据。
    const mine = ++seq;
    const realm = getState().viewRealm;
    const jobs = [
      { key: 'accounts', label: '账号', url: '/accounts?realm=all' },
      { key: 'usage', label: '今日用量', url: '/usage?realm=' + realm + '&range=today' },
      { key: 'trend', label: '调用趋势', url: '/usage/trend?realm=' + realm + '&days=14' },
      { key: 'keys', label: '密钥', url: '/settings' },
      { key: 'scheduler', label: '调度器状态', url: '/scheduler' },
    ];
    const settled = await Promise.allSettled(jobs.map((job) => getJSON(job.url)));
    if (mine !== seq) return;
    const failed = [];
    settled.forEach((result, i) => {
      if (result.status === 'fulfilled') apply(jobs[i].key, result.value);
      else failed.push(jobs[i].label);
    });
    missing = failed;
    // 账号是这一页绝大多数数字的来源；首屏没拿到就整页报错，之后失败只提示并保留旧数据。
    if (accounts === null) {
      fatal = '账号数据没有读取到，仪表盘的数字无法计算';
      phase = 'error';
    } else {
      phase = 'ready';
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
    return trend ? trendSeries(trendMode, trend.buckets, fmtNumber, fmtCompact, { errors: true }) : null;
  }

  function selectTrendMode(id) {
    if (trendMode === id) return;
    trendMode = id;
    draw();
  }

  async function switchExit() {
    const current = getState().activeRealm;
    const next = current === 'intl' ? 'cn' : 'intl';
    const ok = await confirmDialog({
      title: '切换网关默认出口？',
      desc: '切换后，没有绑定版本的密钥会立刻改用' + realmLabel(next) + '账号；已绑定版本的密钥不受影响。这一步会影响所有正在使用网关的客户端。',
      confirmText: '切换到' + realmLabel(next),
      onConfirm: async () => {
        await switchActiveRealm(next);
      },
    });
    if (ok) toast.success('默认出口已切换为' + realmLabel(next));
  }

  async function addAccount() {
    try {
      const mod = await import('./accounts/dialogs/add.js');
      await mod.openAddAccountDialog();
    } catch (err) {
      toastError('打开「添加账号」失败', err);
    }
  }

  function snapshotTile(account, now) {
    const av = availability(account, now);
    const visual = expiryVisual(account, now);
    const bar = creditBar(account, now);
    const credit = creditTone(account, now);
    return html`
      <div class="snap-tile">
        <div class="row-between">
          <span class="snap-name truncate" title=${account.nickname || account.uid}>${account.nickname || account.uid}</span>
          ${badge(av.label, av.tone, { small: true, title: '账号可用性：' + av.label })}
        </div>
        <div class="snap-bar">${bar ? progressBar(bar.pct, bar.color) : progressBar(0, 'var(--border)')}</div>
        <div class="row-between text-2xs">
          <span class="${visual ? 'tone-' + visual.tone : 'muted'} nums">${visual ? (visual.remain > 0 ? '剩余 ' + fmtRemain(visual.remain) : '已过期') : '到期时间未知'}</span>
          <span class="${credit.tone ? 'tone-' + credit.tone : ''} nums">${credit.text}${credit.total ? html`<span class="muted"> / ${credit.total}</span>` : nothing} 积分</span>
        </div>
      </div>`;
  }

  function trendCard() {
    const buckets = trend ? trend.buckets || [] : null;
    const total = trendSummary(buckets);
    return html`
      <div class="card span-2">
        <div class="card-head">
          <h2 class="card-title">${icon('TrendingUp')}近 14 天调用趋势</h2>
          <span class="card-sub">${buckets
            ? '近 14 天请求了 ' + fmtNumber(total.requests) + ' 次，失败 ' + fmtNumber(total.failures) + ' 次，消耗 Token ' + fmtCompact(total.tokens)
            : '数据未读取到'}</span>
          ${buckets ? segmented(TREND_MODES, trendMode, selectTrendMode, 'seg-sm') : nothing}
        </div>
        <div class="dash-chart">
          <canvas ${ref(chartCanvas)}></canvas>
          ${!buckets || !buckets.length
            ? html`<div class="chart-empty text-xs muted">${buckets ? '最近 14 天没有调用记录' : '趋势数据没有读取到，稍后自动重试'}</div>`
            : nothing}
        </div>
      </div>`;
  }

  function exitCard() {
    const realm = getState().activeRealm;
    const next = realm === 'intl' ? 'cn' : 'intl';
    const effect = keyEffect(keys, realm);
    const origin = location.origin;
    const mode = scheduler ? (realm === 'cn' ? scheduler.mode_cn : scheduler.mode_intl) : '';
    return html`
      <div class="card col" style="gap:12px">
        <div class="card-head" style="margin-bottom:0">
          <h2 class="card-title">${icon('Server')}网关出口</h2>
          ${keys ? badge(realmLabel(realm), realm === 'cn' ? 'warning' : 'sky', { small: true }) : nothing}
        </div>
        <p class="card-sub" style="margin:0">没有绑定版本的密钥会走这一个出口，客户端请求由这里的账号接流。</p>
        <div class="kv"><span>当前默认出口</span><b>${realmLabel(realm)}</b></div>
        <div class="kv">
          <span>生效的密钥</span>
          <b>${keys ? effect.total + ' 个' : '—'}</b>
        </div>
        ${keys
          ? html`<div class="card-sub">其中绑定${realmLabel(realm)} ${effect.bound} 个，未绑定版本 ${effect.follow} 个。</div>`
          : html`<div class="card-sub">密钥清单没有读取到，生效数量暂不显示。</div>`}
        <button class="btn btn-outline btn-sm" @click=${switchExit}>${icon('ArrowLeftRight')}切换为${realmLabel(next)}</button>
        <div class="exit-url">
          <code class="break-all">${origin}/v1</code>
          ${copyButton(origin + '/v1', { title: '复制接口地址', label: '复制' })}
        </div>
        <div class="col" style="gap:4px">
          <div class="card-sub">调度器：${scheduler ? (scheduler.enabled ? '运行中' : '已暂停') : '状态未读取到'}</div>
          ${scheduler ? html`<div class="card-sub">下次巡检 ${scheduler.next_run_time || '待调度'}，上次巡检 ${scheduler.last_run_time || '尚未运行'}。</div>` : nothing}
          ${mode ? html`<div class="card-sub">${mode}</div>` : nothing}
          <a class="btn btn-ghost btn-sm" href=${hrefFor('tasks')}>查看任务${icon('ChevronRight')}</a>
        </div>
      </div>`;
  }

  function snapshotCard(list, now) {
    const counts = availabilityCounts(list, now);
    return html`
      <div class="card">
        <div class="card-head">
          <h2 class="card-title">账号健康快照</h2>
          <div class="row-wrap">
            ${counts.map((c) => html`<span class="text-2xs nums tone-${c.tone}">${c.label} ${c.count}</span>`)}
            ${list.length > SNAPSHOT_LIMIT ? html`<a class="btn btn-ghost btn-sm" href=${hrefFor('accounts')}>查看全部账号${icon('ChevronRight')}</a>` : nothing}
          </div>
        </div>
        ${list.length === 0
          ? emptyState({
              icon: 'Users',
              title: '暂无账号',
              desc: realmLabel(getState().viewRealm) + '账号池是空的，添加账号后网关才能接流。',
              action: html`<button class="btn btn-primary btn-sm" @click=${busyClick(addAccount)}>${icon('UserPlus')}添加账号</button>`,
            })
          : html`<div class="snap-grid">${snapshotAccounts(list, SNAPSHOT_LIMIT, now).map((a) => snapshotTile(a, now))}</div>`}
      </div>`;
  }

  function view() {
    if (phase === 'loading') {
      return html`
        <div class="page">
          ${pageHeader({ path: DASH, title: '仪表盘', desc: descText() })}
          ${skeletonStats(5)}
          <div class="grid-3">${skeletonBlock(280)}${skeletonBlock(280)}</div>
          ${skeletonBlock(220)}
        </div>`;
    }
    if (phase === 'error') return html`<div class="page">${pageHeader({ path: DASH, title: '仪表盘', desc: descText() })}${loadErrorPage(fatal || '仪表盘的数据没有读取到', retry)}</div>`;

    const now = Date.now();
    const realm = getState().viewRealm;
    const list = realmAccounts(accounts, realm);
    const summary = accountSummary(list, now);
    const credits = creditsSummary(list, now);
    const hints = usageHints(usage);
    const creditHint = creditsHint(list, now);

    return html`
      <div class="page">
        ${pageHeader({ path: DASH, title: '仪表盘', desc: descText() })}
        ${missing.length
          ? loadErrorInline('这些数据没有刷新成功：' + missing.join('、') + '。下面显示的是上一次读取到的结果。', retry)
          : nothing}
        <div class="grid-stats cols-5">
          ${statCard({ label: '账号总数', value: fmtNumber(summary.total), icon: 'Users', hint: '启用 ' + summary.enabled + ' 个', delay: 0 })}
          ${statCard({ label: '可用账号', value: fmtNumber(summary.ready), icon: 'CircleCheck', tone: summary.attention ? 'warning' : 'success', hint: readyHint(summary), delay: 0.05 })}
          ${statCard({ label: '积分余额', value: credits.known ? fmtNumber(Math.round(credits.total)) : '—', icon: 'Coins', hint: creditHint, delay: 0.1 })}
          ${statCard({ label: '今日请求', value: usage ? fmtNumberDash(usage.requests) : '—', icon: 'Activity', hint: usage ? hints.requestHint : '今日用量未读取到', delay: 0.15 })}
          ${statCard({ label: '今日 Token', value: usage ? fmtCompact(usage.total_tokens || 0) : '—', icon: 'Zap', hint: usage ? hints.tokenHint : '今日用量未读取到', delay: 0.2 })}
        </div>
        <div class="grid-3">
          ${trendCard()}
          ${exitCard()}
        </div>
        ${snapshotCard(list, now)}
      </div>`;
  }

  function descText() {
    return realmLabel(getState().viewRealm) + '账号池健康度、网关出口与今日用量总览。';
  }

  // 右上角切换查看版本时路由会整页卸载再挂载；默认出口的变化只重画这一页。
  const off = subscribe((reason) => {
    if (reason === 'activeRealm') draw();
  });

  draw();
  load();

  return {
    refresh: () => load(),
    unmount() {
      seq += 1;
      off();
      chartHost.destroy();
    },
  };
}
