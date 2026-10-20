// 请求日志页：把 /usage/recent 的记录按时间、密钥、状态等维度筛出来。
// 页面结构参照 running 日志页：筛选卡 + 表格卡，窄屏换成卡片列表，点击任意一行打开详情弹窗。
import { html, render, nothing } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON } from '../core/api.js';
import { getState } from '../core/store.js';
import { readStore, writeStore } from '../core/storage.js';
import { fmtDateTime, fmtDateTimeMarked, fmtLatency, fmtNumber } from '../core/format.js';
import {
  pageHeader, emptyState, loadErrorPage, loadErrorInline, badge, copyButton,
  busyClick, skeletonBlock, skeletonRows, realmBadge,
} from '../ui/widgets.js';
import { openDialog } from '../ui/dialog.js';
import {
  PAGE_SIZES, LIMIT_STORE_KEY, RANGE_OPTIONS, REALM_OPTIONS, STATUS_OPTIONS,
  defaultFilters, normalizeLimit, isFiltered, withFilter, withLimit, withPage, pruneFilters,
  buildRecentQuery, statusInfo, keyLabel, accountLabel, rowTimestamp, cacheHint,
  creditText, effortLabel, pageList, requestDetail,
} from './requests/logic.js';

export const autoRefresh = true;

const PAGE_DESC = '反向代理网关的每一次调用记录，含状态、首字延迟与 Token 计量。';

export function mount(host) {
  const viewRealm = getState().viewRealm;
  let state = {
    filters: defaultFilters(viewRealm),
    baseFilters: defaultFilters(viewRealm),
    page: 1,
    limit: normalizeLimit(readStore(LIMIT_STORE_KEY)),
    rows: [],
    total: 0,
    pages: 1,
    keys: [],
    accounts: [],
    loaded: false,
    error: '',
    optionsError: '',
  };
  let loading = false;
  let pending = false;
  let inputTimer = null;

  const keysById = () => Object.fromEntries(state.keys.map((k) => [k.id, k]));
  const accountsById = () => Object.fromEntries(state.accounts.map((a) => [a.uid, a]));
  function draw() {
    render(view(), host);
    syncFilterControls();
  }

  // lit-html 首次渲染时 select 的 value 可能早于 option 生效，渲染后统一按状态校正一次。
  function syncFilterControls() {
    for (const name of Object.keys(defaultFilters())) {
      const el = host.querySelector('select[data-name="' + name + '"]');
      if (el && el.value !== state.filters[name]) el.value = state.filters[name];
    }
  }

  async function loadOptions() {
    try {
      const [settings, accounts] = await Promise.all([getJSON('/settings'), getJSON('/accounts?realm=all')]);
      state.keys = Array.isArray(settings.api_keys) ? settings.api_keys : [];
      state.accounts = Array.isArray(accounts.accounts) ? accounts.accounts : [];
      state.optionsError = '';
      state.filters = pruneFilters(state.filters, {
        keyIds: ['', 'panel', ...state.keys.map((k) => k.id)],
        accountIds: ['', ...state.accounts.map((a) => a.uid)],
      });
    } catch (err) {
      state.optionsError = '密钥与账号列表没有读取成功，筛选项里暂时看不到它们。' + (err.message ? ' ' + err.message : '');
    }
  }

  async function loadRows() {
    if (loading) {
      pending = true;
      return;
    }
    loading = true;
    try {
      const data = await getJSON(buildRecentQuery(state));
      state.rows = Array.isArray(data.rows) ? data.rows : [];
      state.total = Number(data.total) || 0;
      state.pages = Math.max(1, Math.round(Number(data.pages ?? data.total_pages) || 1));
      if (state.page > state.pages) state.page = state.pages;
      state.error = '';
      state.loaded = true;
    } catch (err) {
      state.error = '请求记录没有读取成功。' + (err.message ? ' ' + err.message : '');
    } finally {
      loading = false;
    }
    draw();
    if (pending) {
      pending = false;
      await loadRows();
    }
  }

  async function reloadAll() {
    await loadOptions();
    await loadRows();
  }

  function setFilter(name, value) {
    if (state.filters[name] === value) return;
    state = withFilter(state, name, value);
    loadRows();
  }

  function setLimit(value) {
    state = withLimit(state, value);
    writeStore(LIMIT_STORE_KEY, state.limit);
    loadRows();
  }

  function gotoPage(page) {
    const next = withPage(state, page);
    if (next.page === state.page) return;
    state = next;
    loadRows();
  }

  function resetFilters() {
    state = { ...state, filters: { ...state.baseFilters }, page: 1 };
    for (const name of ['model', 'ip']) {
      const el = host.querySelector('input[data-name="' + name + '"]');
      if (el) el.value = '';
    }
    loadRows();
  }

  /** 输入框停止输入 300 毫秒后取数。 */
  function onTextInput(name, value) {
    clearTimeout(inputTimer);
    inputTimer = setTimeout(() => setFilter(name, value), 300);
  }

  function openDetail(row, keys, accounts) {
    openDialog({
      width: 620,
      icon: 'ScrollText',
      title: '请求详情',
      desc: `${fmtDateTime(rowTimestamp(row))} · ${row.model || '未知模型'}`,
      body: () => html`
        <div class="detail-list">
          ${requestDetail(row, { keysById: keys, accountsById: accounts }).map((item) => html`
            <div class="detail-row" title=${item.title || nothing}>
              <span class="detail-key">${item.label}</span>
              <span class="detail-value">${item.value}</span>
              ${item.copy ? copyButton(item.copy, { title: '复制' + item.label }) : nothing}
            </div>`)}
        </div>`,
      foot: (dialog) => html`<button class="btn btn-primary" @click=${() => dialog.close()}>关闭</button>`,
    });
  }

  function selectField(label, name, options, value) {
    return html`
      <label class="field">
        <span class="field-label">${label}</span>
        <select class="select" data-name=${name} .value=${value}
                @change=${(event) => setFilter(name, event.target.value)}>
          ${options.map((option) => html`
            <option value=${option.id} ?selected=${option.id === value}>${option.label}</option>`)}
        </select>
      </label>`;
  }

  function filterCard() {
    const keyOptions = [
      { id: '', label: '全部密钥' },
      ...state.keys.map((k) => ({ id: k.id, label: k.name || k.id })),
      { id: 'panel', label: '面板测试台' },
    ];
    const accountOptions = [
      { id: '', label: '全部账号' },
      ...state.accounts.map((a) => ({ id: a.uid, label: a.nickname || a.uid.slice(0, 8) })),
    ];
    return html`
      <div class="card req-filters">
        ${selectField('时间范围', 'range', RANGE_OPTIONS, state.filters.range)}
        ${selectField('版本', 'realm', REALM_OPTIONS, state.filters.realm)}
        ${selectField('密钥', 'key', keyOptions, state.filters.key)}
        ${selectField('状态', 'status', STATUS_OPTIONS, state.filters.status)}
        <label class="field">
          <span class="field-label">模型</span>
          <input class="input" type="search" data-name="model" .value=${state.filters.model}
                 placeholder="模型名的一部分，例如 deepseek"
                 @input=${(event) => onTextInput('model', event.target.value)}>
        </label>
        ${selectField('账号', 'account', accountOptions, state.filters.account)}
        <label class="field">
          <span class="field-label">来源 IP</span>
          <input class="input" type="search" data-name="ip" .value=${state.filters.ip}
                 placeholder="IP 的一部分，例如 192.168"
                 @input=${(event) => onTextInput('ip', event.target.value)}>
        </label>
        ${isFiltered(state.filters, state.baseFilters) ? html`
          <div class="field req-filter-reset">
            <button class="btn btn-outline" @click=${resetFilters}>${icon('RotateCcw')}重置</button>
          </div>` : nothing}
      </div>`;
  }

  function statusCell(row) {
    const status = statusInfo(row);
    return badge(status.text, status.tone, { title: status.title, small: true });
  }

  function tokenCell(row) {
    const cache = cacheHint(row);
    return html`
      <div class="strong nums">${fmtNumber(row.total_tokens)}</div>
      <div class="cell-sub tone-${cache.tone}" title=${cache.title}>${cache.text}</div>`;
  }

  function creditCell(row) {
    const credit = creditText(row);
    return html`<span class=${credit.missing ? 'muted' : 'nums'} title=${credit.title}>${credit.text}</span>`;
  }

  /** 思考档位徽章；老流水与没有档位的模型都没有该字段。 */
  function effortChip(row) {
    const effort = effortLabel(row);
    return effort ? badge(effort, 'violet', { small: true, title: '本次请求实际使用的思考档位' }) : nothing;
  }

  function tableCard() {
    const rows = state.rows;
    const keys = keysById();
    const accounts = accountsById();
    return html`
      <div class="card card-flush">
        <div class="card-head">
          <div class="row">
            <h2 class="card-title">${icon('ScrollText')}请求记录</h2>
            <span class="text-xs muted nums">共 ${fmtNumber(state.total)} 条</span>
          </div>
          <div class="row">
            <span class="text-2xs muted">每页</span>
            <select class="select input-sm" data-name="limit" .value=${String(state.limit)}
                    @change=${(event) => setLimit(event.target.value)}>
              ${PAGE_SIZES.map((n) => html`<option value=${n} ?selected=${n === state.limit}>${n}</option>`)}
            </select>
          </div>
        </div>
        ${rows.length ? html`
          <div class="table-wrap desktop-only">
            <table class="table table-compact">
              <thead>
                <tr>
                  <th>时间</th><th>版本</th><th>密钥</th><th>IP</th><th>账号</th><th>模型</th><th>状态</th>
                  <th class="right">首字</th><th class="right">总耗时</th>
                  <th class="right">Token</th><th class="right">积分</th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((row) => html`
                  <tr class="clickable" @click=${() => openDetail(row, keys, accounts)}>
                    <td class="nums">${fmtDateTimeMarked(rowTimestamp(row))}</td>
                    <td>${row.realm ? realmBadge(row.realm) : html`<span class="muted">—</span>`}</td>
                    <td class="truncate" title=${row.key_id || ''}>${keyLabel(row, keys)}</td>
                    <td class="cell-mono">${row.ip || '未采集'}</td>
                    <td class="truncate" title=${row.account || ''}>${accountLabel(row.account, accounts)}</td>
                    <td class="truncate" title=${row.model || ''}><span class="cell-mono">${row.model || '—'}</span>${effortChip(row)}</td>
                    <td>${statusCell(row)}</td>
                    <td class="right nums">${fmtLatency(row.ttft_ms)}</td>
                    <td class="right nums">${fmtLatency(row.elapsed_ms)}</td>
                    <td class="right">${tokenCell(row)}</td>
                    <td class="right">${creditCell(row)}</td>
                  </tr>`)}
              </tbody>
            </table>
          </div>
          <div class="mobile-list">
            ${rows.map((row) => html`
              <div class="mobile-item" @click=${() => openDetail(row, keys, accounts)}>
                <div class="row-between">
                  <span class="row text-xs strong nums">${fmtDateTimeMarked(rowTimestamp(row))}${row.realm ? realmBadge(row.realm) : nothing}</span>
                  ${statusCell(row)}
                </div>
                <div class="row-between text-2xs muted">
                  <span class="truncate">${keyLabel(row, keys)}</span>
                  <span class="cell-mono">${row.ip || '未采集'}</span>
                </div>
                <div class="row-between">
                  <span class="text-xs truncate">${accountLabel(row.account, accounts)}</span>
                  <span class="text-2xs truncate"><span class="cell-mono">${row.model || '—'}</span>${effortChip(row)}</span>
                </div>
                <div class="row-wrap text-2xs muted nums">
                  <span>首字 ${fmtLatency(row.ttft_ms)}</span>
                  <span>总耗时 ${fmtLatency(row.elapsed_ms)}</span>
                  <span>${fmtNumber(row.total_tokens)} Token</span>
                  ${creditText(row).missing ? nothing : html`<span>${creditText(row).text} 积分</span>`}
                </div>
              </div>`)}
          </div>` : emptyState(state.total === 0 && !isFiltered(state.filters, state.baseFilters) ? {
            icon: 'ScrollText',
            title: '暂无请求记录',
            desc: '当有请求经过网关后，这里会显示调用记录。',
          } : {
            icon: 'FilterX',
            title: '没有匹配的请求记录',
            desc: '换一个关键词，或者点击「重置」清空筛选条件。',
          })}
        ${pagerFooter()}
      </div>`;
  }

  function pagerFooter() {
    const pages = Math.max(1, state.pages);
    return html`
      <div class="pager">
        <span class="nums">共 ${fmtNumber(state.total)} 条 · 第 ${state.page} / ${pages} 页</span>
        <span class="pager-buttons">
          <button class="btn btn-outline btn-icon btn-xs" aria-label="上一页" ?disabled=${state.page <= 1}
                  @click=${() => gotoPage(state.page - 1)}>${icon('ChevronLeft')}</button>
          ${pageList(state.page, pages).map((item) => item === '...'
            ? html`<span class="pager-gap">…</span>`
            : html`
              <button class="btn btn-xs ${item === state.page ? 'btn-primary' : 'btn-outline'}"
                      @click=${() => gotoPage(item)}>${item}</button>`)}
          <button class="btn btn-outline btn-icon btn-xs" aria-label="下一页" ?disabled=${state.page >= pages}
                  @click=${() => gotoPage(state.page + 1)}>${icon('ChevronRight')}</button>
        </span>
      </div>`;
  }

  function view() {
    const actions = html`
      <button class="btn btn-outline btn-sm" @click=${busyClick(reloadAll)}>${icon('RefreshCw')}刷新</button>`;
    if (!state.loaded) {
      return html`
        <div class="page">
          ${pageHeader({ path: 'requests', title: '请求日志', desc: PAGE_DESC, actions })}
          ${state.error ? loadErrorPage(state.error, reloadAll)
            : html`${skeletonBlock(96)}${skeletonRows(8)}`}
        </div>`;
    }
    return html`
      <div class="page">
        ${pageHeader({ path: 'requests', title: '请求日志', desc: PAGE_DESC, actions })}
        ${filterCard()}
        ${state.optionsError ? loadErrorInline(state.optionsError, loadOptions) : nothing}
        ${state.error ? loadErrorInline(state.error, reloadAll) : nothing}
        ${tableCard()}
      </div>`;
  }

  const instance = {
    refresh: async () => { await loadRows(); },
    unmount() {
      clearTimeout(inputTimer);
    },
  };

  draw();
  reloadAll();
  return instance;
}
