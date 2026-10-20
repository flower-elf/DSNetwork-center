import { html, render, nothing, repeat } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON, postJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { fmtAgo, fmtDateTime, realmLabel } from '../core/format.js';
import { toast, toastError } from '../ui/toast.js';
import { pageHeader, statCard, chip, emptyState, loadErrorPage, loadErrorInline,
         skeletonStats, skeletonRows, switchControl, badge, busyClick } from '../ui/widgets.js';
import { CAPABILITIES, LONG_CONTEXT_TOKENS, accountAvailableCount, accountCanUse,
         capabilityBadges, capabilityLabel, contextText, contextTitle, coverageFor,
         creditInfo, disabledIds, fmtTokens, hasCredits, hiddenExcludedCount,
         outputText, panelModelsUrl, realmAccounts, reasoningLevels, selectModels,
         sourceLabel, summary, withEnabled } from './models/logic.js';

// 模型页：目录来自 GET /panel/models，勾选状态写回 settings 的 disabled_models；目录按 5 分钟缓存，只在手动点击时重新读取。
// 账号选择器把所有账号聚合成一列覆盖率，也可以只看单个账号能用的模型。

export function mount(host, ctx) {
  const realm = getState().viewRealm;
  const state = {
    data: null,          // null 表示首屏还没读到数据
    accounts: [],
    scope: 'all',        // all = 聚合视图；其他值是账号 uid
    source: '',
    fetchedAt: null,
    error: '',
    inline: '',          // 刷新失败的原因，保留已有数据
    query: '',
    capability: 'all',
    sort: 'catalog',
    showExcluded: false,
    pending: new Set(),  // 正在保存勾选状态的模型 id
  };
  let saveQueue = Promise.resolve();

  async function load(force) {
    const url = panelModelsUrl(realm, force);
    try {
      const [reply, accountsReply] = await Promise.all([
        getJSON(url),
        getJSON('/accounts?realm=all').catch(() => null),
      ]);
      state.data = reply.data || [];
      if (accountsReply && Array.isArray(accountsReply.accounts)) {
        state.accounts = accountsReply.accounts;
      }
      if (state.scope !== 'all' && !currentAccount()) state.scope = 'all';
      state.source = reply.source || '';
      state.fetchedAt = reply.fetched_at || null;
      state.error = '';
      state.inline = '';
      if (force) {
        toast.success(reply.refresh_ok ? '已从服务器重新获取模型目录'
                                       : '服务器没有返回新的目录，仍在显示上次获取的结果');
      }
    } catch (err) {
      if (state.data === null) state.error = err.message;
      else state.inline = '刷新模型目录失败：' + err.message;
      if (force) toastError('重新获取失败', err);
    }
    draw();
  }

  function realmAccountList() {
    return realmAccounts(state.accounts, realm);
  }

  function currentAccount() {
    if (state.scope === 'all') return null;
    return realmAccountList().find((a) => a.uid === state.scope) || null;
  }

  const refresh = () => load(true);

  async function toggleModel(id, enabled) {
    if (state.pending.has(id)) return;
    state.data = withEnabled(state.data, id, enabled);
    state.pending.add(id);
    draw();
    // 两次快速勾选各自读到整份 disabled_models；让保存按点击顺序排队，
    // 后一次永远基于前一次已提交的状态，避免旧清单把新改动覆盖回去。
    const job = saveQueue.then(async () => {
      let reply = null;
      try {
        reply = await postJSON('/settings/save', { disabled_models: disabledIds(state.data) });
        if (enabled) toast.success('已开始处理 ' + id);
        else toast.warn('已停止处理 ' + id);
      } catch (err) {
        state.data = withEnabled(state.data, id, !enabled);
        toastError('保存模型处理状态失败', err);
      } finally {
        state.pending.delete(id);
        // 以服务端整理后的清单为准；同时还有别的勾选在保存时不覆盖它，等那一次的结果
        if (reply && !state.pending.size) {
          const disabled = new Set((reply.disabled_models || []).map((v) => String(v).toLowerCase()));
          state.data = state.data.map((m) => ({ ...m, enabled: !disabled.has(m.id.toLowerCase()) }));
        }
        draw();
      }
    });
    saveQueue = job.catch(() => {});
    return job;
  }

  function sourceLine() {
    const parts = ['数据来源：' + sourceLabel(state.source)];
    if (state.fetchedAt) parts.push(`获取时间：${fmtDateTime(state.fetchedAt)}，${fmtAgo(state.fetchedAt)}`);
    return html`<div class="text-2xs muted">${parts.join(' · ')}</div>`;
  }

  function staleNotice() {
    if (state.source === 'server' || state.source === 'cache') return nothing;
    return html`<div class="notice notice-warn" role="status">${icon('TriangleAlert')}
      <div class="grow">没有从上游取到模型目录，当前显示的是${sourceLabel(state.source)}。请确认账号可用，或点右上角「重新获取」再试一次。</div></div>`;
  }

  function stats() {
    const s = summary(state.data);
    const account = currentAccount();
    const available = account ? accountAvailableCount(state.data, account) : s.enabled;
    const maxOutHint = s.maxOutput ? `单次最多输出 ${fmtTokens(s.maxOutput)}` : nothing;
    return html`
      <div class="grid-stats cols-4">
        ${statCard(account
          ? { label: '该账号可用模型', value: available, icon: 'Boxes',
              hint: `共 ${s.total} 个 · 全局开启 ${s.enabled} 个`, delay: 0 }
          : { label: '可用模型', value: s.total, icon: 'Boxes',
              hint: `网关处理中 ${s.enabled} 个`, hintTone: s.enabled < s.total ? 'warning' : '', delay: 0 })}
        ${statCard({ label: '支持推理', value: s.reasoning, icon: 'Brain', hint: '目录里带思考档位的模型', delay: 0.05 })}
        ${statCard({ label: '长上下文模型', value: s.longContext, icon: 'Maximize2', hint: `上下文不少于 ${fmtTokens(LONG_CONTEXT_TOKENS)} 的模型`, delay: 0.1 })}
        ${statCard({ label: '最长上下文', value: fmtTokens(s.maxContext), icon: 'Gauge', hint: maxOutHint, delay: 0.15 })}
      </div>`;
  }

  function filterCard() {
    const hidden = hiddenExcludedCount(state.data);
    const creditSort = hasCredits(state.data);
    return html`
      <div class="card card-tight models-filter">
        <label class="row" style="gap:6px">
          <span class="chips-label">${icon('Users')}查看</span>
          <select class="select input-sm" title="全部账号是聚合视图，显示每个模型在多少个已启用账号里可用"
                  .value=${state.scope}
                  @change=${(event) => { state.scope = event.target.value; draw(); }}>
            <option value="all" .selected=${state.scope === 'all'}>全部账号（聚合）</option>
            ${realmAccountList().map((account) => html`
              <option value=${account.uid} .selected=${state.scope === account.uid}>
                ${account.nickname || String(account.uid || '').slice(0, 8)}${account.enabled === false ? '（已停用）' : ''}
              </option>`)}
          </select>
        </label>
        <label class="input-search models-search">
          ${icon('Search')}
          <input class="input" type="search" placeholder="搜索模型名 / 模型 ID / 说明"
                 .value=${state.query}
                 @input=${(e) => { state.query = e.target.value; draw(); }}>
        </label>
        <div class="chips">
          <span class="chips-label">${icon('Shapes')}能力</span>
          ${CAPABILITIES.map((c) => chip(c.label, state.capability === c.id, () => {
            state.capability = c.id;
            draw();
          }))}
          ${creditSort ? html`
            <span class="chips-sep"></span>
            ${chip('倍率从低到高', state.sort === 'credit', () => {
              state.sort = state.sort === 'credit' ? 'catalog' : 'credit';
              draw();
            })}
            ${chip('上下文从大到小', state.sort === 'context', () => {
              state.sort = state.sort === 'context' ? 'catalog' : 'context';
              draw();
            })}` : nothing}
        </div>
        <div class="models-filter-tail">
          <span class="text-2xs muted">${hidden ? `${hidden} 个被排除的模型默认不显示` : ''}</span>
          ${switchControl({
            checked: state.showExcluded,
            label: '显示被排除的模型',
            onChange: (value) => { state.showExcluded = value; draw(); },
          })}
          <span class="text-xs">显示被排除的模型</span>
        </div>
      </div>`;
  }

  function creditCell(m) {
    const info = creditInfo(m, realm);
    if (!info) return html`<span class="muted">—</span>`;
    return html`
      <span class="row" style="gap:6px">
        ${info.free ? badge(info.label, 'success', { title: '上游把倍率标成 0，按免费计费' }) : badge(info.label, 'muted')}
        ${info.note ? html`<span class="text-3xs tone-violet nowrap">${info.note}</span>` : nothing}
      </span>`;
  }

  function modelCell(m) {
    return html`
      <div class="model-cell">
        <div class="row-wrap" style="gap:6px">
          <span class="strong">${m.name || m.id}</span>
          ${m.excluded ? badge('被排除', 'warning', { small: true, title: '上游目录里列出了它，但筛选规则把它排除了；它不会出现在 /v1/models，需要拒绝请求时请取消勾选' }) : nothing}
          ${capabilityBadges(m).map((b) => badge(b.label, b.tone, { small: true, title: b.title }))}
        </div>
        ${m.name && m.name !== m.id ? html`<span class="mono text-2xs muted break-all">${m.id}</span>` : nothing}
      </div>`;
  }

  function enableSwitch(m) {
    const busy = state.pending.has(m.id);
    return switchControl({
      checked: !!m.enabled,
      disabled: busy,
      label: m.enabled ? '已开启处理' : '未处理',
      onChange: (value) => toggleModel(m.id, value),
    });
  }

  function effortCell(m) {
    const levels = reasoningLevels(m);
    if (!levels.length) return html`<span class="muted">—</span>`;
    return html`<span class="row-wrap" style="gap:4px">${levels.map((level) => html`<span class="effort">${level}</span>`)}</span>`;
  }

  function accountCell(m, account) {
    if (!m.enabled) {
      return badge('全局停用', 'danger', { small: true, title: '网关已停止处理这个模型，任何账号都不会接单' });
    }
    if (!accountCanUse(m, account)) {
      return badge('已排除', 'warning', { small: true, title: '这个账号不接这个模型，请求会交给其他账号' });
    }
    return badge('可用', 'success', { small: true });
  }

  function coverageCell(m) {
    const coverage = coverageFor(m, realmAccountList());
    return html`<span class="nums ${coverage.can === 0 ? 'tone-warning' : ''}"
                      title="已启用账号中可以接这个模型的数量">${coverage.text}</span>`;
  }

  function table(rows) {
    const account = currentAccount();
    return html`
      <div class="table-wrap desktop-only">
        <table class="table">
          <thead>
            <tr>
              <th>模型</th>
              <th>上下文</th>
              <th>最大输出</th>
              <th>思考档位</th>
              <th>积分倍率</th>
              ${account ? html`<th class="center">该账号</th>` : html`<th>账号可用</th>`}
              ${account ? nothing : html`<th class="center">启用</th>`}
            </tr>
          </thead>
          <tbody>
            ${repeat(rows, (m) => m.id, (m) => html`
              <tr class=${m.enabled ? '' : 'row-off'}>
                <td>${modelCell(m)}</td>
                <td class="nums" title=${contextTitle(m) || nothing}>${contextText(m)}</td>
                <td class="nums">${outputText(m)}</td>
                <td>${effortCell(m)}</td>
                <td>${creditCell(m)}</td>
                ${account ? html`<td class="center">${accountCell(m, account)}</td>`
                          : html`<td>${coverageCell(m)}</td>`}
                ${account ? nothing : html`<td class="center">${enableSwitch(m)}</td>`}
              </tr>`)}
          </tbody>
        </table>
      </div>
      <div class="mobile-list">
        ${rows.map((m) => html`
          <div class="mobile-item ${m.enabled ? '' : 'row-off'}">
            <div class="row-between">
              <div class="grow">${modelCell(m)}</div>
              ${account ? accountCell(m, account) : enableSwitch(m)}
            </div>
            ${account ? nothing : html`<div class="kv"><span>账号可用</span><span>${coverageCell(m)}</span></div>`}
            <div class="kv"><span>上下文</span><span>${contextText(m)}</span></div>
            <div class="kv"><span>最大输出</span><span>${outputText(m)}</span></div>
            <div class="kv"><span>思考档位</span><span>${effortCell(m)}</span></div>
            <div class="kv"><span>积分倍率</span><span>${creditCell(m)}</span></div>
          </div>`)}
      </div>`;
  }

  function body() {
    const rows = selectModels(state.data, state);
    const filtered = state.query.trim() || state.capability !== 'all';
    return html`
      ${sourceLine()}
      ${stats()}
      ${filterCard()}
      <div class="card card-flush">
        ${rows.length ? table(rows) : emptyState(filtered
          ? { icon: 'SearchX', title: '没有匹配的模型', desc: '换个关键词，或把能力筛选切回「全部」；被排除的模型需要在筛选条里打开显示。' }
          : { icon: 'Boxes', title: '暂无模型', desc: '没有从上游或内置表读到任何模型，请确认账号可用，然后点右上角「重新获取」。' })}
        <div class="models-foot">
          <span>已筛选出 ${rows.length} 个，共 ${(state.data || []).length} 个</span>
          <span>${capabilityLabel(state.capability)}${state.query.trim() ? ` · 关键词「${state.query.trim()}」` : ''}</span>
        </div>
      </div>`;
  }

  function view() {
    const actions = html`
      <button class="btn btn-outline btn-sm" title="重新获取模型目录。平时按 5 分钟缓存，避免频繁请求触发风控。"
              @click=${busyClick(refresh)}>${icon('RefreshCw')}重新获取</button>`;
    return html`
      <div class="page" aria-busy=${!!state.pending.size}>
        ${pageHeader({
          path: 'models',
          title: '模型中心',
          desc: `${realmLabel(realm)}账号可用的模型、上下文长度与思考档位；选择单个账号查看它能接的模型，全部账号显示每个模型的账号覆盖率。取消勾选后，网关会直接拒绝这个模型的请求`,
          actions,
        })}
        ${state.error ? loadErrorPage('读取模型目录失败：' + state.error, () => load(false))
          : state.data === null ? html`${skeletonStats(4)}${skeletonRows(6)}`
          : html`${state.inline ? loadErrorInline(state.inline, refresh) : nothing}${staleNotice()}${body()}`}
      </div>`;
  }

  function draw() {
    render(view(), host);
  }

  draw();
  load(false);
  return { refresh: () => load(false) };
}
