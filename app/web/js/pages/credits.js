// 积分页：按账号查看积分构成，套餐按来源归并、可以逐笔展开。
// 距离上次读取超过设置的间隔才重新读取，停留期间不做定时刷新。
import { html, nothing, render } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { refreshSeconds } from '../core/refresh.js';
import { fmtDateTime, fmtNumber, realmLabel } from '../core/format.js';
import { badge, busyClick, emptyState, loadErrorInline, loadErrorPage, pageHeader, realmBadge, skeletonStats, statCard } from '../ui/widgets.js';
import { toast, toastError } from '../ui/toast.js';
import * as acct from './accounts/actions.js';
import { accountsForRealm } from './accounts/logic.js';
import {
  accountCredits, creditRowKey, cycleText, dueForRefresh, expiryChip, expiringTotal, groupSubText, nextExpiry, packageState, pageTotals, stateBadge, unqueriedAccounts,
} from './credits/logic.js';

// 本会话里最近一次成功读取的时间与结果，供「打开本页是否要重新读取」判断。
let lastRefreshAt = 0;
let cachedAccounts = null;

export function mount(host) {
  const S = {
    accounts: [],
    loading: true,
    error: '',
    partial: '',
    open: new Set(),   // 展开的归并行：来源键
    tried: new Set(),  // 已经补查过余额的账号，每页只补一次
  };

  function draw() {
    render(view(), host);
  }

  async function refresh() {
    let payload;
    try {
      payload = await getJSON('/accounts?realm=all');
    } catch (err) {
      if (!S.accounts.length) {
        S.error = err.message || '读取账号失败';
        S.loading = false;
        draw();
        return;
      }
      S.partial = '账号列表这次没有读取成功，下面显示的是上一次的数据：' + err.message;
      S.loading = false;
      draw();
      return;
    }
    S.accounts = payload.accounts || [];
    S.error = '';
    S.partial = '';
    S.loading = false;
    lastRefreshAt = Date.now();
    cachedAccounts = S.accounts;
    draw();
    fillMissingCredits();
  }

  /** 刚导入的账号还没有余额，进入本页补查一次；每个账号只查一次，避免反复请求整个账号池。 */
  async function fillMissingCredits() {
    const todo = unqueriedAccounts(realmAccounts(), S.tried);
    if (!todo.length) return;
    for (const account of todo) S.tried.add(account.uid);
    try {
      for (const account of todo) {
        const result = await acct.fetchCredits(account.uid);
        if (result.accounts) S.accounts = result.accounts;
      }
      draw();
    } catch (err) {
      S.partial = '有账号的积分没有刷新成功：' + err.message;
      draw();
    }
  }

  async function refreshCredits() {
    try {
      const result = await acct.refreshCredits(getState().viewRealm);
      if (result.accounts) S.accounts = result.accounts;
      const failed = (result.results || []).filter((row) => !row.ok);
      if (failed.length) toast.error('部分账号余额没有读取到', failed.length + ' 个账号刷新失败，稍后可以重试。');
      else toast.success('已刷新' + realmLabel(getState().viewRealm) + '账号的积分');
      draw();
    } catch (err) {
      toastError('刷新积分失败', err);
    }
  }

  async function fetchOne(account) {
    try {
      const result = await acct.fetchCredits(account.uid);
      if (result.accounts) S.accounts = result.accounts;
      draw();
    } catch (err) {
      toastError('刷新「' + (account.nickname || account.uid) + '」的余额失败', err);
    }
  }

  function realmAccounts() {
    return accountsForRealm(S.accounts, getState().viewRealm);
  }

  function toggleGroup(key) {
    if (S.open.has(key)) S.open.delete(key);
    else S.open.add(key);
    draw();
  }

  /** 归并行的到期列：最近一笔可用额度的倒计时；还剩过期条目或已用完时标出来。 */
  function groupExpiry(group, now) {
    if (group.nextExpireAt) {
      const chip = expiryChip(group.nextExpireAt, group.remain, now);
      return html`<span class="tone-${chip.tone} text-xs" title=${chip.title}>${chip.text}</span>`;
    }
    if (group.state === 'expired') {
      return group.lastExpireAt
        ? html`<span class="tone-danger text-xs" title="这批额度的最后到期时间">${fmtDateTime(group.lastExpireAt)}</span>`
        : badge('已过期', 'danger', { small: true });
    }
    if (group.state === 'used') return html`<span class="text-2xs muted">已用完</span>`;
    return html`<span class="text-2xs muted">无到期时间</span>`;
  }

  function groupRow(account, group, now) {
    const state = stateBadge(group.state);
    const rowKey = creditRowKey(account, group);
    const open = S.open.has(rowKey);
    const expandable = group.count > 1;
    return html`
      <tr class=${expandable ? 'clickable' : ''}
          @click=${expandable ? () => toggleGroup(rowKey) : nothing}>
        <td>
          <div class="credit-source">
            ${expandable ? icon(open ? 'ChevronDown' : 'ChevronRight') : icon('Gift')}
            <span class="strong">${group.name}</span>
          </div>
          ${groupSubText(group) ? html`<div class="cell-sub">${groupSubText(group)}</div>` : nothing}
        </td>
        <td class="right nums">${fmtNumber(group.size)}</td>
        <td class="right nums">${fmtNumber(group.used)}</td>
        <td class="right nums strong">${fmtNumber(group.remain)}</td>
        <td>${groupExpiry(group, now)}</td>
        <td>${badge(state.text, state.tone, { small: true })}</td>
      </tr>
      ${open && expandable ? group.packages.map((item, index) => packageRow(item, index, now)) : nothing}`;
  }

  function packageRow(item, index, now) {
    const state = stateBadge(packageState(item, now));
    return html`
      <tr class="credit-sub">
        <td>
          <div class="text-xs">第 ${index + 1} 笔</div>
          <div class="cell-sub">${cycleText(item) || '上游没有给出周期'}</div>
        </td>
        <td class="right nums">${fmtNumber(item.size)}</td>
        <td class="right nums">${fmtNumber(item.used)}</td>
        <td class="right nums">${fmtNumber(item.remain)}</td>
        <td class="text-xs">${item.expireAt ? fmtDateTime(item.expireAt) : '—'}</td>
        <td>${badge(state.text, state.tone, { small: true })}</td>
      </tr>`;
  }

  function accountCard(account, now) {
    const data = accountCredits(account, now);
    const expiring = expiringTotal([account], 15, now);
    const name = account.nickname || String(account.uid || '').slice(0, 8) || '账号';
    return html`
      <div class="card card-flush">
        <div class="card-head">
          <div>
            <h2 class="card-title">${name}${realmBadge(account.realm)}</h2>
            <p class="card-sub">
              ${data.queried
                ? html`剩余 ${fmtNumber(data.remain)} · 共 ${fmtNumber(data.size)} · 已用 ${fmtNumber(data.used)} · 15 天内到期 ${fmtNumber(expiring)}${data.age ? ' · ' + data.age : ''}`
                : '还没有查询过这个账号的余额'}
            </p>
          </div>
          <button class="btn btn-outline btn-sm" @click=${busyClick(() => fetchOne(account))}>
            ${icon('RefreshCw')}刷新
          </button>
        </div>
        ${data.queried && data.groups.length ? html`
          <div class="table-wrap">
            <table class="table table-compact">
              <thead>
                <tr>
                  <th>来源</th>
                  <th class="right">总额</th>
                  <th class="right">已用</th>
                  <th class="right">剩余</th>
                  <th>最近到期</th>
                  <th>状态</th>
                </tr>
              </thead>
              <tbody>${data.groups.map((group) => groupRow(account, group, now))}</tbody>
            </table>
          </div>`
          : html`<div class="card-body-note">${data.queried
              ? '上游这次没有返回套餐明细，只有合计余额。'
              : '点「刷新」从上游读取这个账号的积分构成。'}</div>`}
      </div>`;
  }

  function summaryCards(now) {
    const totals = pageTotals(realmAccounts());
    const expiry = nextExpiry(realmAccounts(), now);
    const expiring = expiringTotal(realmAccounts(), 15, now);
    return html`
      <div class="grid-stats cols-4">
        ${statCard({ label: '积分余额', value: fmtNumber(totals.remain), icon: 'Coins', tone: 'success', hint: totals.queried + ' 个账号已查到余额' })}
        ${statCard({ label: '已用积分', value: fmtNumber(totals.used), icon: 'TrendingDown', hint: '按上游返回的累计用量' })}
        ${statCard({ label: '15 天内到期积分', value: fmtNumber(expiring), icon: 'CalendarClock', tone: expiring ? 'warning' : '', hint: '按套餐剩余额度统计，不含已过期与无到期时间的额度' })}
        ${statCard({ label: '最近到期', value: expiry ? fmtNumber(expiry.amount) : '—', icon: 'CalendarClock', tone: expiry ? 'warning' : '',
          hint: expiry ? expiry.name + ' · ' + expiry.account + ' · ' + fmtNumber(expiry.amount) + ' 积分' : '没有带到期时间的额度' })}
      </div>`;
  }

  function view() {
    const realm = getState().viewRealm;
    const now = Date.now();
    const accounts = realmAccounts();
    if (S.loading) {
      return html`<div class="page">${header(realm)}${skeletonStats(4)}</div>`;
    }
    if (S.error) {
      return html`<div class="page">${header(realm)}${loadErrorPage('读取账号失败：' + S.error, refresh)}</div>`;
    }
    return html`
      <div class="page">
        ${header(realm)}
        ${S.partial ? loadErrorInline(S.partial, refresh) : nothing}
        ${accounts.length ? html`
          ${summaryCards(now)}
          <div class="col" style="gap:12px">${accounts.map((account) => accountCard(account, now))}</div>`
          : emptyState({
              icon: 'Coins',
              title: '还没有' + realmLabel(realm) + '账号',
              desc: '添加账号后，这里会按账号显示积分来源。',
            })}
      </div>`;
  }

  function header(realm) {
    return pageHeader({
      path: 'credits',
      title: '积分',
      desc: realmLabel(realm) + '账号的积分构成：每一笔的来源、已用与剩余，以及到期时间；打开本页时距离上次读取超过面板选项里设置的间隔才会重新读取',
      actions: html`
        <button class="btn btn-outline btn-sm" @click=${busyClick(refreshCredits)}>
          ${icon('RefreshCw')}刷新全部
        </button>`,
    });
  }

  if (dueForRefresh(lastRefreshAt, Date.now(), refreshSeconds())) {
    draw();
    refresh();
  } else {
    S.accounts = cachedAccounts;
    S.loading = false;
    draw();
  }

  return {
    refresh: async () => { await refresh(); },
    unmount: () => {},
  };
}
