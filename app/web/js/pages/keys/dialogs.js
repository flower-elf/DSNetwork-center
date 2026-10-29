// 密钥页的弹窗：新建 / 编辑表单，以及创建成功后的接入信息。
import { html, nothing, live } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { getJSON } from '../../core/api.js';
import { openDialog } from '../../ui/dialog.js';
import { toastError } from '../../ui/toast.js';
import { copyButton } from '../../ui/widgets.js';
import { copyWithToast } from '../../ui/copy.js';
import {
  EXPIRY_MODES_EDIT,
  EXPIRY_MODES_NEW,
  REALM_CHOICES,
  addPatterns,
  apiBaseUrl,
  buildKeyEntry,
  clientSnippets,
  formFromRow,
  randomKeyValue,
  removePatternAt,
  validateKeyForm,
} from './logic.js';

/**
 * 新建 / 编辑密钥；saveEntry(entry) 由页面提供，提交失败必须抛出，弹窗据此显示错误并保持打开。
 * onCreated(entry) 只在新建成功后调用，用明文密钥打开接入信息弹窗。
 */
export function openKeyDialog({ row = null, saveEntry, onCreated }) {
  const form = formFromRow(row);
  let error = '';
  let busy = false;
  // 出口 -> 启用中的模型 ID；下拉只列出这些模型。
  const enabledModels = {};
  let modelMenuOpen = false;
  let modelQuery = '';
  let modelActive = -1;

  /** 当前出口可选的模型：绑定出口只列该出口，跟随默认出口时取两个出口的并集，已添加的不再列出。 */
  function pickableModels() {
    const realms = form.realm ? [form.realm] : ['intl', 'cn'];
    const ids = new Set();
    realms.forEach((realm) => (enabledModels[realm] || []).forEach((id) => ids.add(id)));
    return [...ids].filter((id) => !form.patterns.includes(id)).sort();
  }

  function matchingModels() {
    const query = modelQuery.trim().toLowerCase();
    const models = pickableModels();
    return query ? models.filter((id) => id.toLowerCase().includes(query)) : models;
  }

  function addModel(raw) {
    form.patterns = addPatterns(form.patterns, raw);
    form.patternsTouched = true;
    modelQuery = '';
    modelMenuOpen = false;
    modelActive = -1;
    ctl.update();
  }

  function onPatternKeydown(event) {
    const matches = matchingModels();
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      modelMenuOpen = true;
      modelActive = matches.length ? Math.min(modelActive + 1, matches.length - 1) : -1;
      ctl.update();
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      modelActive = Math.max(modelActive - 1, 0);
      ctl.update();
      return;
    }
    if (event.key === 'Escape') {
      modelMenuOpen = false;
      ctl.update();
      return;
    }
    if (event.key !== 'Enter' && event.key !== ',') return;
    event.preventDefault();
    const picked = modelActive >= 0 && matches[modelActive];
    addModel(picked || event.target.value);
  }

  function modelSuggestions() {
    const matches = matchingModels();
    return html`
      <div class="model-suggest" role="listbox" aria-label="启用中的模型">
        ${matches.length ? matches.map((id, index) => html`
          <button type="button" class="model-option ${index === modelActive ? 'active' : ''}"
                  role="option" aria-selected=${index === modelActive}
                  @mousedown=${(e) => e.preventDefault()}
                  @click=${() => addModel(id)}>${id}</button>`)
        : html`<div class="model-empty">没有匹配的启用模型</div>`}
      </div>`;
  }

  async function loadModels() {
    const realms = (form.realm ? [form.realm] : ['intl', 'cn']).filter((realm) => !enabledModels[realm]);
    try {
      const lists = await Promise.all(realms.map((realm) => getJSON('/v1/models?realm=' + realm)));
      realms.forEach((realm, index) => {
        enabledModels[realm] = ((lists[index] && lists[index].data) || []).map((m) => m.id).filter(Boolean);
      });
    } catch (err) {
      toastError('模型列表没有读取成功，只能手工输入模型名', err);
    }
    ctl.update();
  }

  const ctl = openDialog({
    width: 580,
    icon: 'KeyRound',
    title: form.editing ? '编辑密钥' : '新建密钥',
    desc: form.editing
      ? '修改名称、出口绑定、模型限制、有效期、配额与 IP 白名单；密钥内容留空表示不改动。'
      : '密钥创建后仍可在列表里复制。留空的字段表示不限制。',
    body: () => html`
      <div class="grid-2 key-form-grid">
        <label class="field">
          <span class="field-label">名称</span>
          <input class="input" id="keyFormName" type="text" placeholder="例如：客服组 / Cursor / 本地测试"
                 .value=${form.name} @input=${(e) => { form.name = e.target.value; }}>
        </label>
        <label class="field">
          <span class="field-label">出口绑定</span>
          <select class="select" .value=${live(form.realm)} @change=${(e) => { form.realm = e.target.value; ctl.update(); loadModels(); }}>
            ${REALM_CHOICES.map((c) => html`<option value=${c.value} ?selected=${form.realm === c.value}>${c.label}</option>`)}
          </select>
          <span class="field-hint">绑定后，用这把密钥发起的请求固定走所选出口；跟随面板默认出口表示按网关当前出口。</span>
        </label>
      </div>

      <div class="field">
        <span class="field-label">
          密钥内容
          ${form.editing ? html`<span class="muted">当前 ${form.masked || '已保存'}，留空表示不修改</span>` : nothing}
        </span>
        <div class="row key-value-row">
          <input class="input grow" id="keyFormValue" type="text" autocomplete="off" spellcheck="false"
                 placeholder=${form.editing ? '留空表示保持原密钥不变' : '填写自定义密钥，或点击右侧「随机生成」'}
                 .value=${form.keyValue} @input=${(e) => { form.keyValue = e.target.value; }}>
          <button class="btn btn-outline btn-sm" type="button" @click=${() => { form.keyValue = randomKeyValue(); ctl.update(); }}>随机生成</button>
        </div>
        <span class="field-hint">至少 4 个字符。</span>
      </div>

      <div class="field">
        <span class="field-label">模型限制</span>
        <div class="tag-editor">
          ${form.patterns.map((pattern, index) => html`
            <span class="tag">${pattern}
              <button type="button" class="tag-remove" title="移除" @click=${() => { form.patterns = removePatternAt(form.patterns, index); form.patternsTouched = true; ctl.update(); }}>${icon('X')}</button>
            </span>`)}
          <div class="tag-input-wrap">
            <input class="tag-input" id="keyFormPattern" type="text" autocomplete="off"
                   placeholder=${form.patterns.length ? '输入模型名后按回车' : '输入模型名后按回车，留空表示不限制，接受全部模型'}
                   .value=${live(modelQuery)}
                   @focus=${() => { modelMenuOpen = true; modelActive = -1; ctl.update(); }}
                   @blur=${() => { if (modelMenuOpen) { modelMenuOpen = false; ctl.update(); } }}
                   @input=${(e) => {
                     modelQuery = e.target.value;
                     modelMenuOpen = true;
                     modelActive = -1;
                     ctl.update();
                   }}
                   @keydown=${onPatternKeydown}>
            ${modelMenuOpen ? modelSuggestions() : nothing}
          </div>
        </div>
        <span class="field-hint">点击输入框可以从下拉里选择启用中的模型；也支持 * 通配，例如 deepseek*，按回车添加。点标签上的叉号移除。${form.editing && !form.patternsKnown ? ' 当前版本的服务端没有返回已保存的模型限制：留空表示保持原值，添加标签后保存会覆盖原值。' : ''}</span>
      </div>

      <div class="field">
        <span class="field-label">有效期</span>
        <div class="seg seg-sm">
          ${(form.editing ? EXPIRY_MODES_EDIT : EXPIRY_MODES_NEW).map((mode) => html`
            <button type="button" class="seg-item ${form.expiryMode === mode.value ? 'active' : ''}"
                    @click=${() => { form.expiryMode = mode.value; ctl.update(); }}>${mode.label}</button>`)}
        </div>
        ${form.expiryMode === 'days' ? html`
          <div class="row">
            <input class="input input-num" id="keyFormExpiryDays" type="number" min="1" max="3650" step="1"
                   .value=${String(form.expiryDays)} @input=${(e) => { form.expiryDays = e.target.value; }}>
            <span class="text-xs muted">天后到期${form.editing ? '，从现在起算' : ''}</span>
          </div>` : nothing}
        <span class="field-hint">过期的密钥调用 /v1 接口会收到 401；到期后可以随时改成永不过期或延长天数。</span>
      </div>

      <div class="grid-2 key-form-grid">
        <label class="field">
          <span class="field-label">配额 Token</span>
          <input class="input input-num" id="keyFormQuotaTokens" type="number" min="0" step="1000" placeholder="0 表示不限"
                 .value=${form.quotaTokens} @input=${(e) => { form.quotaTokens = e.target.value; }}>
          <span class="field-hint">从零开始累计的 Token 上限，用满后调用会收到 429。</span>
        </label>
        <label class="field">
          <span class="field-label">配额积分</span>
          <input class="input input-num" id="keyFormQuotaCredit" type="number" min="0" step="0.01" placeholder="0 表示不限"
                 .value=${form.quotaCredit} @input=${(e) => { form.quotaCredit = e.target.value; }}>
          <span class="field-hint">按上游返回的真实扣费累计，与 Token 配额各自独立，任一用满即停止服务。</span>
        </label>
      </div>

      <label class="field">
        <span class="field-label">IP 白名单</span>
        <textarea class="textarea" id="keyFormIpAllowlist" rows="3" placeholder="每行一个，例如 203.0.113.7 或 10.0.0.0/8"
                  .value=${form.ipText} @input=${(e) => { form.ipText = e.target.value; }}></textarea>
        <span class="field-hint">每行一个 IP 或 CIDR；留空表示不限来源 IP。不在名单内的来源会收到 403。</span>
      </label>

      ${form.editing && form.usageStart ? html`
        <p class="field-hint">用量自 ${new Date(form.usageStart * 1000).toLocaleString('zh-CN')} 起累计：${form.usedTokens} token · ${form.usedCredit} 积分。</p>` : nothing}
      ${error ? html`<p class="field-error" role="alert">${error}</p>` : nothing}`,
    foot: () => html`
      <button class="btn btn-ghost" ?disabled=${busy} @click=${() => ctl.close()}>取消</button>
      <button class="btn btn-primary" ?disabled=${busy} @click=${save}>
        ${busy ? icon('LoaderCircle', 'spin') : nothing}${form.editing ? '保存修改' : '创建密钥'}
      </button>`,
  });
  loadModels();

  async function save() {
    error = validateKeyForm(form);
    if (error) {
      ctl.update();
      return;
    }
    // 明文只在本地生成或由操作员填写，提交后服务端也只回掩码，这里先留存一份。
    const entry = buildKeyEntry(form);
    busy = true;
    ctl.update();
    try {
      await saveEntry(entry);
    } catch (err) {
      busy = false;
      ctl.update();
      toastError('保存失败', err);
      return;
    }
    busy = false;
    ctl.close();
    if (!form.editing && onCreated) onCreated(entry);
  }

  return ctl;
}

/** 创建成功后的接入信息：明文密钥、Base URL 与常用客户端的配置片段。 */
export function openCreatedKeyDialog({ entry, origin }) {
  const key = entry.key;
  const base = apiBaseUrl(origin);
  const snippets = clientSnippets(origin, key);
  return openDialog({
    width: 620,
    icon: 'CircleCheck',
    title: '密钥创建成功',
    desc: '下面是这次创建的密钥与接入配置。',
    body: () => html`
      <div class="col" style="gap:12px">
        <div class="key-plain">
          <code class="grow break-all">${key}</code>
          ${copyButton(key, { label: '复制密钥', title: '复制密钥' })}
        </div>
        <div class="setting">
          <div class="setting-main">
            <div class="setting-name">Base URL</div>
            <div class="setting-desc">OpenAI 兼容接口的地址，客户端把它填进 base_url。</div>
          </div>
          <div class="setting-control">
            <code class="text-xs break-all">${base}</code>
            ${copyButton(base, { label: '复制 Base URL', title: '复制 Base URL' })}
          </div>
        </div>
        ${snippets.map((snippet) => html`
          <div class="snippet">
            <div class="row-between">
              <span class="text-xs medium">${snippet.title}</span>
              ${copyButton(snippet.text, { label: '复制', title: '复制这段配置' })}
            </div>
            <div class="text-2xs muted">${snippet.hint}</div>
            <pre class="snippet-code scroll-slim">${snippet.text}</pre>
          </div>`)}
      </div>`,
    foot: (dialog) => html`<button class="btn btn-primary" @click=${() => dialog.close()}>我已保存</button>`,
  });
}

/** 复制列表里的密钥：掩码不含明文，先经 /settings/reveal 读取值再写剪贴板。 */
export async function revealKeyToClipboard(id) {
  const data = await getJSON('/settings/reveal?id=' + encodeURIComponent(id));
  const value = String((data && data.key) || '').trim();
  if (!value) throw new Error('服务端没有返回这把密钥的内容');
  await copyWithToast(value, '密钥已复制到剪贴板');
}
