// 密钥页：对外网关的分发密钥列表与增删改。
import { html, render, nothing } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON, postJSON } from '../core/api.js';
import { confirmDialog } from '../ui/dialog.js';
import { toast, toastError } from '../ui/toast.js';
import {
  badge,
  emptyState,
  loadErrorInline,
  loadErrorPage,
  pageHeader,
  skeletonRows,
} from '../ui/widgets.js';
import {
  STATUS_HINT,
  STATUS_TEXT,
  STATUS_TONE,
  authSummary,
  buildSavePayload,
  deriveKeyStatus,
  expiryHint,
  expiryText,
  ipLimitText,
  keyDisplay,
  keyRowPayload,
  lastUsedText,
  limitTitle,
  modelLimitText,
  quotaExceeded,
  realmBindingText,
  summarizeKeys,
  startupKeyRow,
  usageLines,
} from './keys/logic.js';
import { openCreatedKeyDialog, openKeyDialog, revealKeyToClipboard } from './keys/dialogs.js';

export const autoRefresh = true;

export function mount(host, ctx) {
  const state = {
    view: null,
    rows: [],
    loading: true,
    loaded: false,
    error: '',
  };

  function draw() {
    render(view(), host);
  }

  function applyView(data) {
    state.view = data;
    state.rows = Array.isArray(data.api_keys) ? data.api_keys : [];
    state.error = '';
    state.loading = false;
    state.loaded = true;
    draw();
  }

  async function load({ quiet = false } = {}) {
    if (!state.loaded && !quiet) {
      state.loading = true;
      draw();
    }
    try {
      applyView(await getJSON('/settings'));
    } catch (err) {
      state.loading = false;
      state.error = err.message;
      draw();
      if (state.loaded) toastError('读取密钥列表失败', err);
    }
  }

  /** 提交整份密钥列表；失败把错误抛给调用方。 */
  async function submit(payload, successText) {
    const data = await postJSON('/settings/save', payload);
    applyView(data);
    if (successText) toast.success(successText);
  }

  // 整份列表是最后写入者胜出：两次操作如果同时读旧 state，后到的旧清单
  // 会把先前的改动覆盖回去。所有写操作排队执行，排到时再读当前 state。
  let saveQueue = Promise.resolve();
  function enqueue(job) {
    const run = saveQueue.then(job);
    saveQueue = run.catch(() => {});
    return run;
  }

  /** 目标行在列表里的下标，找不到返回 -1。 */
  function rowIndex(id) {
    return state.rows.findIndex((row) => (row.id || '') === (id || ''));
  }

  async function saveEntry(entry) {
    return enqueue(async () => {
      const rows = state.rows.map(keyRowPayload);
      const index = rowIndex(entry.id);
      if (index >= 0) rows[index] = entry;
      else rows.push(entry);
      // 显式空删除列表 = upsert：另一个标签页新增的密钥不会被这份旧列表带走。
      await submit(buildSavePayload(rows));
    });
  }

  function openCreate() {
    openKeyDialog({
      row: null,
      saveEntry,
      onCreated: (entry) => openCreatedKeyDialog({ entry, origin: location.origin }),
    });
  }

  function openEdit(row) {
    openKeyDialog({ row, saveEntry });
  }

  async function copyRow(row) {
    if (!row.id) {
      toast.warn('这一行还没有保存');
      return;
    }
    try {
      await revealKeyToClipboard(row.id);
    } catch (err) {
      toastError('复制密钥失败', err);
    }
  }

  function toggleRow(row, index) {
    const enabled = row.enabled !== false;
    const apply = () => enqueue(async () => {
      const rows = state.rows.map(keyRowPayload);
      const at = rowIndex(row.id);
      const target = at >= 0 ? at : index;
      if (target < 0 || target >= rows.length) throw new Error('这一行已经不在列表里');
      rows[target] = { ...rows[target], enabled: !enabled };
      await submit(buildSavePayload(rows),
                   enabled ? `已停用密钥「${row.name || '未命名'}」` : `已启用密钥「${row.name || '未命名'}」`);
    });
    if (!enabled) {
      // 启用不需要确认；失败在这里给提示，避免未处理的 Promise 拒绝。
      apply().catch((err) => toastError('启用密钥失败', err));
      return;
    }
    confirmDialog({
      title: '停用这个密钥？',
      desc: `停用后，使用密钥「${row.name || '未命名'}」的客户端会立即收到 401。随时可以再启用。`,
      confirmText: '停用',
      danger: true,
      onConfirm: apply,
    });
  }

  function resetUsage(row) {
    if (!row.id) {
      toast.warn('这一行还没有保存');
      return;
    }
    confirmDialog({
      title: '重置用量？',
      desc: `将把密钥「${row.name || '未命名'}」的 Token 与积分统计清零，配额从零开始计算。已经发生的调用记录不会删除。`,
      confirmText: '重置',
      onConfirm: () => enqueue(
        () => submit({ reset_key_usage: row.id }, `已重置密钥「${row.name || '未命名'}」的用量`)),
    });
  }

  function removeRow(row) {
    confirmDialog({
      title: '删除密钥',
      desc: `将删除密钥「${row.name || '未命名'}」。使用它的客户端会立即收到 401，此操作无法撤销。`,
      confirmText: '删除',
      danger: true,
      onConfirm: () => enqueue(async () => {
        const keep = state.rows.filter((candidate) => (
          row.id ? (candidate.id || '') !== row.id : candidate !== row));
        const payload = buildSavePayload(keep);
        payload.deleted_api_key_ids = row.id ? [row.id] : [];
        await submit(payload, '密钥已删除');
      }),
    });
  }

  function statusBadge(row) {
    const status = deriveKeyStatus(row);
    return badge(STATUS_TEXT[status], STATUS_TONE[status], { title: STATUS_HINT[status] });
  }

  function limitCell(row) {
    return html`
      <div class="col" style="gap:2px" title=${limitTitle(row)}>
        <span class="text-xs">${ipLimitText(row)}</span>
        <span class="text-2xs muted">${modelLimitText(row)}</span>
      </div>`;
  }

  function usageCell(row) {
    const [first, quota] = usageLines(row);
    const exceeded = quotaExceeded(row);
    return html`
      <div class="col" style="gap:2px">
        <span class="text-xs nums">${first}</span>
        ${quota ? html`<span class="text-2xs ${exceeded ? 'tone-danger' : 'muted'} nums">${quota}</span>` : nothing}
      </div>`;
  }

  function actions(row, index) {
    return html`
      <button class="btn btn-ghost btn-icon btn-xs" title="复制密钥" @click=${() => copyRow(row)}>${icon('Copy')}</button>
      <button class="btn btn-ghost btn-icon btn-xs" title="编辑" @click=${() => openEdit(row)}>${icon('Pencil')}</button>
      <button class="btn btn-ghost btn-icon btn-xs" title=${row.enabled === false ? '启用' : '停用'}
              @click=${() => toggleRow(row, index)}>${icon(row.enabled === false ? 'CircleCheck' : 'Ban')}</button>
      <button class="btn btn-ghost btn-icon btn-xs" title="重置用量" @click=${() => resetUsage(row)}>${icon('RotateCcw')}</button>
      <button class="btn btn-ghost btn-icon btn-xs tone-danger" title="删除" @click=${() => removeRow(row)}>${icon('Trash2')}</button>`;
  }

  function noticeBlock() {
    const summary = authSummary(state.view);
    const kind = summary.kind === 'none' || summary.kind === 'open' || summary.kind === 'locked' ? 'warn' : 'info';
    const startup = startupKeyRow(state.view);
    return html`
      <div class="col" style="gap:8px">
        <div class="notice notice-${kind}">${icon(kind === 'warn' ? 'TriangleAlert' : 'ShieldCheck')}
          <div class="grow">${summary.text}</div>
        </div>
        ${startup ? html`
          <div class="setting key-startup-row">
            <div class="setting-main">
              <div class="setting-name">${startup.name}${badge('只读', 'muted', { small: true })}</div>
              <div class="setting-desc">${startup.note}</div>
            </div>
            <div class="setting-control">
              <code class="text-xs">${startup.masked}</code>
            </div>
          </div>` : nothing}
      </div>`;
  }

  function table(rows) {
    return html`
      <div class="card card-flush">
        <div class="table-wrap desktop-only">
          <table class="table">
            <thead>
              <tr>
                <th>名称</th>
                <th>密钥</th>
                <th>状态</th>
                <th>出口绑定</th>
                <th>有效期</th>
                <th>限制</th>
                <th>已用</th>
                <th>最近使用</th>
                <th class="actions">操作</th>
              </tr>
            </thead>
            <tbody>
              ${rows.map((row, index) => html`
                <tr>
                  <td>
                    <div class="col" style="gap:2px">
                      <span class="text-sm medium">${row.name || '未命名'}</span>
                      <span class="text-2xs muted">${index + 1} · ${row.created_at || '创建时间未知'}</span>
                    </div>
                  </td>
                  <td><code class="cell-mono">${keyDisplay(row)}</code></td>
                  <td>${statusBadge(row)}</td>
                  <td><span class="text-xs">${realmBindingText(row.realm)}</span></td>
                  <td>
                    <div class="col" style="gap:2px">
                      <span class="text-xs nums">${expiryText(row)}</span>
                      ${Number(row.expires_at) ? html`<span class="text-2xs muted">${expiryHint(row)}</span>` : nothing}
                    </div>
                  </td>
                  <td>${limitCell(row)}</td>
                  <td>${usageCell(row)}</td>
                  <td><span class="text-xs">${lastUsedText(row)}</span></td>
                  <td class="actions">${actions(row, index)}</td>
                </tr>`)}
            </tbody>
          </table>
        </div>
        <div class="mobile-list">
          ${rows.map((row, index) => html`
            <div class="mobile-item">
              <div class="row-between">
                <span class="text-sm medium">${row.name || '未命名'}</span>
                ${statusBadge(row)}
              </div>
              <div class="row-wrap text-2xs muted">
                <code class="cell-mono">${keyDisplay(row)}</code>
                <span>·</span>
                <span>${realmBindingText(row.realm)}</span>
              </div>
              <div class="row-wrap text-2xs muted">
                <span>${expiryText(row)}</span>
                <span>·</span>
                <span>${ipLimitText(row)}</span>
                <span>·</span>
                <span>${modelLimitText(row)}</span>
              </div>
              <div class="col" style="gap:2px">
                ${[usageLines(row)[0], usageLines(row)[1]].filter(Boolean).map((line) => html`<span class="text-2xs nums muted">${line}</span>`)}
                <span class="text-2xs muted">最近使用 ${lastUsedText(row)}</span>
              </div>
              <div class="row-wrap">${actions(row, index)}</div>
            </div>`)}
        </div>
      </div>`;
  }

  function view() {
    const summary = summarizeKeys(state.rows);
    const actionsRow = html`
      <button class="btn btn-outline btn-sm" @click=${() => load()}>${icon('RefreshCw')}刷新</button>
      <button class="btn btn-primary btn-sm" @click=${openCreate}>${icon('Plus')}新建密钥</button>`;

    if (state.loading) {
      return html`
        <div class="page">
          ${pageHeader({ path: 'keys', title: 'API 密钥', desc: desc(), actions: actionsRow })}
          ${skeletonRows(5)}
        </div>`;
    }
    if (!state.loaded) {
      return html`
        <div class="page">
          ${pageHeader({ path: 'keys', title: 'API 密钥', desc: desc(), actions: actionsRow })}
          ${loadErrorPage('读取密钥设置失败：' + (state.error || '未知原因'), () => load())}
        </div>`;
    }
    return html`
      <div class="page">
        ${pageHeader({ path: 'keys', title: 'API 密钥', desc: desc(), actions: actionsRow })}
        ${state.error ? loadErrorInline('部分数据刷新失败：' + state.error, () => load({ quiet: true })) : nothing}
        ${noticeBlock()}
        ${state.rows.length
          ? table(state.rows)
          : html`<div class="card">${emptyState({
              icon: 'KeyRound',
              title: '暂无 API 密钥',
              desc: '创建一个密钥，客户端即可用 OpenAI SDK 或 Claude Code 调用本网关。',
              action: html`<button class="btn btn-primary btn-sm" @click=${openCreate}>${icon('Plus')}新建密钥</button>`,
            })}</div>`}
        ${state.rows.length ? html`
          <p class="text-2xs muted">共 ${summary.total} 个密钥：正常 ${summary.ok} 个，已停用 ${summary.disabled} 个，已过期 ${summary.expired} 个，超配额 ${summary.quota_exceeded} 个。</p>` : nothing}
      </div>`;
  }

  function desc() {
    return '对外网关的分发密钥，支持出口绑定、有效期、IP 白名单、模型限制与配额。';
  }

  load();

  return {
    refresh: async () => {
      // 后台刷新不清空已有内容；失败时保留旧数据并给出提示。
      await load({ quiet: state.loaded });
    },
    unmount() {},
  };
}
