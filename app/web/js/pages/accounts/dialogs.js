// 账号页的弹窗：添加账号、导入账号、备注、桌面客户端扫描；菜单栏的「添加账号」直接调用这里导出的 openAddAccountDialog。

import { html, nothing, unsafeSVG } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { getState } from '../../core/store.js';
import { qrcode } from '../../../vendor/qrcode-generator-2.0.4.mjs';
import { confirmDialog, openDialog } from '../../ui/dialog.js';
import { busyClick, notice, segmented } from '../../ui/widgets.js';
import { toast, toastError } from '../../ui/toast.js';
import { copyWithToast } from '../../ui/copy.js';
import * as acct from './actions.js';
import { PRODUCTS, productLabel, modelSelection, deniedModels } from './logic.js';

const REALM_LABEL = { cn: '国内版 (www.workbuddy.cn)', intl: '国际版 (www.workbuddy.ai)' };
const REALM_NAME = { cn: '国内版', intl: '国际版' };

/** 二维码只用 SVG 一种形态，按链接缓存，重新生成链接时才重算。 */
const qrCache = new Map();

function qrSvg(text) {
  const hit = qrCache.get(text);
  if (hit) return hit;
  const qr = qrcode(0, 'M');
  qr.addData(text);
  qr.make();
  const svgText = qr.createSvgTag({ cellSize: 4, margin: 8, scalable: true, alt: '授权链接二维码' });
  if (qrCache.size > 8) qrCache.clear();
  qrCache.set(text, svgText);
  return svgText;
}

/**
 * 添加账号 (OAuth)：默认选中当前查看的版本与 WorkBuddy 桌面端，链接生成后展示二维码与可复制链接，
 * 每 2 秒轮询一次登录状态，关闭弹窗时通知服务端取消本次登录。登录所用的客户端就是新账号的出站身份。
 */
export function openAddAccountDialog(options = {}) {
  const initial = options.realm === 'cn' || options.realm === 'intl' ? options.realm : getState().viewRealm;
  let realm = initial;
  let product = 'workbuddy';
  let mode = options.mode === 'apikey' ? 'apikey' : 'oauth';
  let apiKey = '';
  let apiNickname = '';
  let apiBusy = false;
  let apiError = '';
  let phase = 'idle';        // idle | starting | waiting | ok | failed
  let state = '';
  let authUrl = '';
  let loginDomain = '';
  let message = '';
  let failures = 0;
  let timer = null;
  let closed = false;

  const ctl = openDialog({
    title: () => (mode === 'apikey' ? '添加 API Key 账号' : '添加账号'),
    desc: () => (mode === 'apikey'
      ? '粘贴企业版控制台签发的静态 Key。添加前会先验证企业额度，验证失败不会写入账号池。'
      : '先选择要登录的版本和客户端，再打开链接或用手机扫码完成授权。授权完成后账号会自动加入账号池，所选客户端就是这个账号的出站身份。'),
    width: 460,
    body: () => html`
      <div class="realm-choice">
        <span class="field-label">添加方式</span>
        ${segmented([
          { id: 'oauth', label: 'OAuth 授权' },
          { id: 'apikey', label: 'API Key' },
        ], mode, switchMode)}
      </div>
      ${mode === 'apikey' ? html`
        <label class="field">
          <span class="field-label">API Key</span>
          <input class="input" type="password" autocomplete="off" spellcheck="false"
                 placeholder="ck_..." .value=${apiKey}
                 @input=${(event) => { apiKey = event.target.value; }}>
          <span class="field-hint">企业版 CodeBuddy 控制台签发，形如 ck_&lt;id&gt;.&lt;secret&gt;；只适用于国内版。</span>
        </label>
        <label class="field">
          <span class="field-label">备注名（可选）</span>
          <input class="input" maxlength="100" placeholder="例如：设计团队" .value=${apiNickname}
                 @input=${(event) => { apiNickname = event.target.value; }}>
        </label>
        ${apiError ? notice('warn', apiError) : nothing}
        <p class="text-2xs muted">验证通过后出站身份默认 CLI，可在账号行切换。</p>
      ` : html`
      <div class="realm-choice">
        <span class="field-label">登录到哪个版本</span>
        ${segmented([
          { id: 'cn', label: REALM_LABEL.cn },
          { id: 'intl', label: REALM_LABEL.intl },
        ], realm, switchRealm)}
      </div>
      <div class="realm-choice">
        <span class="field-label">以哪个官方客户端登录</span>
        ${segmented(PRODUCTS.map((item) => ({ id: item.id, label: item.short })), product, switchProduct)}
        <span class="text-2xs muted">${productLabel(product)}</span>
      </div>
      <div class="step-list">
        <div class="step ${stepClass(1)}">
          <span class="step-n">1</span>
          <div class="step-t">${stepOneText()}</div>
        </div>
        <div class="step ${stepClass(2)}">
          <span class="step-n">2</span>
          <div class="step-t">在浏览器里打开下面的链接，或者用手机扫描二维码，登录并同意授权</div>
        </div>
        ${authUrl ? html`
          <div class="qr-panel">
            <div class="qr-image">${unsafeSVG(qrSvg(authUrl))}</div>
            <div class="qr-side">
              <div class="field-label">授权链接</div>
              <div class="link-box" title=${authUrl}>${authUrl}</div>
              <div class="row-wrap">
                <button class="btn btn-outline btn-sm" @click=${() => copyWithToast(authUrl, '授权链接已复制')}>${icon('Copy')}复制链接</button>
                <a class="btn btn-outline btn-sm" href=${authUrl} target="_blank" rel="noopener">${icon('ExternalLink')}打开链接</a>
              </div>
              <div class="text-2xs muted">手机号登录也可以，扫码后按浏览器里的提示操作即可。</div>
            </div>
          </div>` : nothing}
        <div class="step ${stepClass(3)}">
          <span class="step-n">3</span>
          <div class="step-t">等待授权回调，本弹窗每 2 秒自动检测一次</div>
        </div>
        ${message ? html`<p class="step-message ${phase === 'failed' ? 'tone-warning' : 'muted'}">${message}</p>` : nothing}
      </div>`}`,
    foot: () => (mode === 'apikey' ? html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>关闭</button>
      <button class="btn btn-primary" ?disabled=${apiBusy} @click=${busyClick(commitApiKey)}>
        ${apiBusy ? icon('LoaderCircle', 'spin') : icon('KeyRound')}验证并添加
      </button>` : html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>关闭</button>
      <button class="btn btn-primary" ?disabled=${phase === 'starting'} @click=${busyClick(() => startLogin())}>
        ${phase === 'starting' ? icon('LoaderCircle', 'spin') : icon('RefreshCw')}重新生成链接
      </button>`),
    // 弹窗一关就停止轮询并通知服务端取消这次登录，避免留下没人认领的登录窗口。
    onClose: () => {
      closed = true;
      stopPolling();
      void cancelCurrent();
    },
  });

  function stepClass(index) {
    if (index === 1) return phase === 'starting' ? 'active' : phase === 'idle' ? 'active' : 'done';
    if (index === 2) return phase === 'waiting' ? 'active' : phase === 'ok' || phase === 'failed' ? 'done' : '';
    return phase === 'ok' ? 'done' : phase === 'waiting' || phase === 'failed' ? 'active' : '';
  }

  function stepOneText() {
    if (phase === 'starting') return '正在向 ' + REALM_NAME[realm] + ' 申请授权链接';
    if (phase === 'failed') return '申请授权链接没有成功，可以点下方「重新生成链接」再试';
    if (loginDomain) {
      return '已向 ' + REALM_NAME[realm] + ' (' + loginDomain + ') 申请授权链接';
    }
    return '已向 ' + REALM_NAME[realm] + ' 申请授权链接';
  }

  function stopPolling() {
    if (timer) {
      clearInterval(timer);
      timer = null;
    }
  }

  // 关闭弹窗或切换版本时通知服务端放弃这次登录；失败要提示，否则服务端会一直保留这个登录窗口。
  function cancelCurrent() {
    if (!state) return Promise.resolve();
    const pending = state;
    state = '';
    return acct.cancelLogin(pending).catch((err) => toastError('取消登录状态失败', err));
  }

  async function switchRealm(next) {
    if (next === realm) return;
    realm = next;
    await startLogin();
  }

  async function switchProduct(next) {
    if (next === product) return;
    product = next;
    await startLogin();
  }

  async function startLogin() {
    stopPolling();
    await cancelCurrent();
    authUrl = '';
    loginDomain = '';
    message = '';
    failures = 0;
    phase = 'starting';
    ctl.update();
    try {
      const started = await acct.startLogin(realm, product);
      state = started.state;
      authUrl = started.authUrl;
      loginDomain = started.domain || '';
      phase = 'waiting';
      message = '正在等待授权完成，完成后会自动加入账号池。';
      ctl.update();
      timer = setInterval(poll, 2000);
    } catch (err) {
      phase = 'failed';
      message = '发起失败：' + err.message;
      ctl.update();
    }
  }

  async function poll() {
    if (!state || closed) return;
    try {
      const result = await acct.pollLogin(state);
      failures = 0;
      message = result.message || result.status || '';
      if (result.status === 'ok') {
        stopPolling();
        state = '';
        phase = 'ok';
        const nickname = (result.account && result.account.nickname) || '新账号';
        message = '登录成功：' + nickname + ' 已加入账号池。';
        ctl.update();
        toast.success('账号已加入账号池', nickname);
        acct.notifyAccountsChanged();
        setTimeout(() => { if (!closed) ctl.close(); }, 1500);
        return;
      }
      if (result.status === 'error' || result.status === 'expired' || result.status === 'unknown') {
        stopPolling();
        phase = 'failed';
      }
      ctl.update();
    } catch (err) {
      // 单次轮询失败多为网络抖动，连续三次失败才提示。
      failures += 1;
      if (failures >= 3) {
        message = '轮询暂时失败，仍在重试：' + err.message;
        ctl.update();
      }
    }
  }

  async function switchMode(next) {
    if (next === mode) return;
    mode = next;
    apiError = '';
    ctl.update();
    if (mode === 'apikey') {
      stopPolling();
      await cancelCurrent();
      phase = 'idle';
      authUrl = '';
      loginDomain = '';
      message = '';
      ctl.update();
      return;
    }
    await startLogin();
  }

  async function commitApiKey() {
    const key = apiKey.trim();
    if (!key) {
      apiError = '请先填入 API Key';
      ctl.update();
      return;
    }
    apiBusy = true;
    apiError = '';
    ctl.update();
    try {
      const result = await acct.addApiKey(key, apiNickname.trim());
      const probe = (result && result.probe) || {};
      const usage = probe.used != null
        ? '，已用 ' + probe.used + (probe.limit != null ? ' / ' + probe.limit : '')
        : '';
      toast.success('API Key 账号已加入账号池',
        (result && result.nickname ? result.nickname : '新账号') + usage);
      acct.notifyAccountsChanged();
      apiKey = '';
      ctl.close();
    } catch (err) {
      apiError = err.message || '验证失败，账号没有写入';
    } finally {
      apiBusy = false;
      ctl.update();
    }
  }

  ctl.dismissible = true;
  if (mode === 'oauth') startLogin();
  return ctl;
}

/** 导入账号：先预览服务端的校验结果，确认后再写盘。 */
export function openImportDialog({ onDone } = {}) {
  let doc = null;
  let rows = null;
  let fileName = '';
  let preview = null;
  let overwrite = false;
  let busy = false;
  let error = '';

  const ctl = openDialog({
    title: '导入账号',
    desc: '选择一个账号 JSON 文件。支持本网关导出的文件、账号数组、单个账号对象，以及桌面客户端保存的凭证文件。',
    width: 640,
    body: () => html`
      <div class="row-between">
        <div class="grow">
          <div class="text-sm medium">${fileName || '尚未选择文件'}</div>
          <div class="text-2xs muted">${preview ? '下面是这份文件的校验结果，此时还没有写入任何内容。' : '选择文件后先预览结果，点「确认导入」才会写入。'}</div>
        </div>
        <button class="btn btn-outline btn-sm" ?disabled=${busy} @click=${busyClick(() => pickFile())}>${icon('FolderOpen')}选择文件</button>
      </div>
      ${error ? notice('warn', error) : nothing}
      ${preview ? previewHtml(preview) : nothing}
      <label class="row text-xs" style="gap:8px">
        <input type="checkbox" .checked=${overwrite} @change=${(e) => { overwrite = e.target.checked; ctl.update(); }}>
        覆盖同 UID 的现有账号，包含它的凭证
      </label>`,
    foot: () => html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>取消</button>
      <button class="btn btn-primary" ?disabled=${busy || !canCommit()} @click=${busyClick(() => commit())}>
        ${busy ? icon('LoaderCircle', 'spin') : icon('Upload')}${commitLabel()}
      </button>`,
  });

  function canCommit() {
    return !!rows && !!preview && ((preview.added || []).length + (preview.updated || []).length) > 0;
  }

  function commitLabel() {
    if (!preview) return '确认导入';
    const count = (preview.added || []).length + (preview.updated || []).length;
    return count ? '确认导入 ' + count + ' 个' : '没有可导入的账号';
  }

  function previewHtml(result) {
    const added = result.added || [];
    const updated = result.updated || [];
    const skipped = result.skipped || [];
    const invalid = result.invalid || [];
    const section = (title, list, tone) => (list.length ? html`
      <div class="preview-group">
        <div class="text-xs medium ${tone ? 'tone-' + tone : ''}">${title} ${list.length}</div>
        <ul class="preview-list">
          ${list.map((item) => {
            const uid = typeof item === 'string' ? item : item.uid;
            const reason = typeof item === 'object' && item.reason ? ' — ' + item.reason : '';
            return html`<li><code>${String(uid || '?').slice(0, 8)}</code>${reason}</li>`;
          })}
        </ul>
      </div>` : nothing);
    return html`
      <div class="row-wrap text-xs">
        <span>新增 <b class="tone-success">${added.length}</b></span>
        <span>覆盖 <b>${updated.length}</b></span>
        <span>跳过 <b>${skipped.length}</b></span>
        <span>无法解析 <b class="tone-warning">${invalid.length}</b></span>
      </div>
      ${section('将新增', added, 'success')}
      ${section('将覆盖', updated, '')}
      ${section('将跳过', skipped, 'muted')}
      ${section('无法解析', invalid, 'warning')}
      ${!added.length && !updated.length ? html`
        <p class="text-xs tone-warning">没有可导入的账号。${skipped.length ? '若要覆盖已经存在的账号，请勾选下方的「覆盖同 UID 的现有账号」。' : ''}</p>` : nothing}`;
  }

  function pickFile() {
    // 每次点击都新建一个文件选择框，这样连续选同一个文件也会触发。
    const picker = document.createElement('input');
    picker.type = 'file';
    picker.accept = '.json,application/json';
    picker.onchange = () => {
      const file = picker.files && picker.files[0];
      if (file) readFile(file);
    };
    picker.click();
  }

  function parseDoc(text) {
    try {
      return JSON.parse(text);
    } catch {
      throw new Error('文件不是合法的 JSON');
    }
  }

  async function readFile(file) {
    busy = true;
    error = '';
    ctl.update();
    try {
      doc = parseDoc(await file.text());
      rows = doc;
      fileName = file.name;
      preview = await acct.importPreview(doc).then((response) => response.result || {});
    } catch (err) {
      doc = null;
      rows = null;
      preview = null;
      fileName = file.name || '';
      error = '读取失败：' + err.message;
    } finally {
      busy = false;
      ctl.update();
    }
  }

  async function commit() {
    if (!rows) return;
    if (overwrite) {
      const go = await confirmDialog({
        title: '覆盖同 UID 的现有账号？',
        desc: '同 UID 的账号会被文件里的凭证整体替换，原有令牌与备注之外的信息以文件为准。',
        confirmText: '继续导入',
        danger: true,
      });
      if (!go) return;
    }
    busy = true;
    ctl.update();
    try {
      const response = await acct.importCommit(rows, overwrite);
      const result = response.result || {};
      const n = (result.added || []).length + (result.updated || []).length;
      toast.success('导入完成', '新增 ' + (result.added || []).length
        + ' 个，覆盖 ' + (result.updated || []).length
        + ' 个，跳过 ' + (result.skipped || []).length
        + ' 个，无法解析 ' + (result.invalid || []).length + ' 个');
      acct.notifyAccountsChanged();
      if (onDone) onDone(result);
      if (n > 0) {
        ctl.close();
        return;
      }
      preview = result;
    } catch (err) {
      toastError('导入失败', err);
    } finally {
      busy = false;
      ctl.update();
    }
  }

  return ctl;
}

/** 账号备注：只保存在本机账号文件里，留空即清除。 */
export function openNoteDialog(account, { onDone, limits } = {}) {
  const maxNote = Number(limits && limits.noteMax) || 100;
  let value = String(account.note || '');
  let busy = false;
  const name = account.nickname || String(account.uid || '').slice(0, 8);

  const ctl = openDialog({
    title: '账号备注',
    desc: '给「' + name + '」写一句只有你看得懂的备注，例如「张叔叔」「备用号」。留空保存即清除。',
    width: 400,
    body: () => html`
      <label class="field">
        <span class="field-label">备注内容</span>
        <input class="input" autofocus maxlength=${maxNote} placeholder="例如：张叔叔" .value=${value}
               @input=${(e) => { value = e.target.value; }}>
        <span class="field-hint">最长 ${maxNote} 字，只保存在本机，不会写进上游。</span>
      </label>`,
    foot: () => html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>取消</button>
      <button class="btn btn-primary" ?disabled=${busy} @click=${busyClick(save)}>${icon('Check')}保存备注</button>`,
  });

  async function save() {
    const text = value.trim();
    if ([...text].length > maxNote) {
      toast.warn('备注太长了', '备注最长 ' + maxNote + ' 字');
      return;
    }
    busy = true;
    ctl.update();
    try {
      await acct.saveNote(account.uid, text);
      toast.success(text ? '备注已保存' : '备注已清除', name);
      acct.notifyAccountsChanged();
      if (onDone) onDone(text);
      ctl.close();
    } catch (err) {
      toastError('保存备注失败', err);
    } finally {
      busy = false;
      ctl.update();
    }
  }

  return ctl;
}

export function openModelExcludeDialog(account, { onDone } = {}) {
  const name = account.nickname || String(account.uid || '').slice(0, 8);
  let ids = null;
  let picked = new Set();
  let error = '';
  let busy = false;

  const ctl = openDialog({
    title: '模型排除',
    desc: '「' + name + '」不会接这些模型的请求，其他账号不受影响。取消勾选即排除，全部勾选表示不排除任何模型。',
    width: 560,
    body: () => html`
      ${error ? notice('warn', error) : nothing}
      ${ids === null
        ? html`<p class="text-xs muted">正在读取模型目录…</p>`
        : html`
          <div class="row-wrap" style="margin-bottom:10px">
            <span class="text-xs muted">共 ${ids.length} 个模型，已排除 ${ids.length - picked.size} 个</span>
            <span class="grow"></span>
            <button class="btn btn-outline btn-sm" @click=${() => setAll(true)}>全选</button>
            <button class="btn btn-outline btn-sm" @click=${() => setAll(false)}>全不选</button>
          </div>
          <div class="model-picker scroll-slim">
            ${ids.map((id) => html`
              <label title=${id}>
                <input type="checkbox" .checked=${picked.has(id)}
                       @change=${(event) => toggle(id, event.target.checked)}>
                <code>${id}</code>
              </label>`)}
          </div>`}`,
    foot: () => html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>取消</button>
      <button class="btn btn-primary" ?disabled=${busy || ids === null || !!error}
              @click=${busyClick(save)}>${icon('Check')}保存排除</button>`,
  });

  async function load() {
    try {
      const payload = await acct.loadRealmModels(account.realm);
      const list = ((payload && payload.data) || []).map((item) => item.id).filter(Boolean);
      const view = modelSelection(list, account.disabledModels);
      ids = view.ids;
      picked = view.picked;
      error = '';
    } catch (err) {
      error = '模型目录没有读取成功：' + err.message;
    }
    ctl.update();
  }

  function toggle(id, on) {
    if (on) picked.add(id);
    else picked.delete(id);
    ctl.update();
  }

  function setAll(on) {
    picked = on ? new Set(ids || []) : new Set();
    ctl.update();
  }

  async function save() {
    busy = true;
    ctl.update();
    try {
      const deny = deniedModels(ids, picked);
      await acct.setDisabledModels(account.uid, deny);
      toast.success(deny.length ? '已排除 ' + deny.length + ' 个模型' : '已取消模型排除', name);
      acct.notifyAccountsChanged();
      if (onDone) onDone();
      ctl.close();
    } catch (err) {
      toastError('保存模型排除失败', err);
    } finally {
      busy = false;
      ctl.update();
    }
  }

  load();
  return ctl;
}

/**
 * 桌面客户端账号扫描。客户端自 2026-09-24 起把令牌改成加密存储，扫到的凭证无法使用，
 * 导入后请求都会失败，因此页面上没有入口；代码与后端接口保留，等解密打通或改走 OAuth 后再放回。
 */
export function openDesktopScanDialog() {
  let detected = null;
  let poolUids = new Set();
  let error = '';
  let busy = false;

  const ctl = openDialog({
    title: '本机桌面客户端账号',
    desc: '只读检测本机桌面客户端已经登录的账号，点「导入」才会加入网关。国际版账号归入国际版列表，国内版账号归入国内版列表。',
    width: 720,
    body: () => html`
      ${notice('info', '桌面客户端自 2026-09-24 起把令牌改成加密存储，扫描到的凭证可能无法直接使用。这个入口暂时不在页面上显示，保留给排查问题时使用。')}
      ${error ? notice('warn', error) : nothing}
      ${detected === null ? html`<p class="text-xs muted">正在检测本机桌面客户端账号…</p>` : scanHtml()}`,
    foot: () => html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>关闭</button>
      <button class="btn btn-outline" ?disabled=${busy} @click=${busyClick(() => scan())}>
        ${busy ? icon('LoaderCircle', 'spin') : icon('RefreshCw')}重新扫描
      </button>`,
  });

  function section(title, items) {
    return html`
      <div class="scan-group">
        <div class="text-xs medium">${title} · ${items.length} 个</div>
        ${items.length ? html`
          <div class="table-wrap">
            <table class="table table-compact">
              <thead><tr><th>账号</th><th>域名</th><th>有效期</th><th class="actions"></th></tr></thead>
              <tbody>
                ${items.map((item) => html`
                  <tr>
                    <td>
                      <div class="medium">${item.nickname || String(item.uid || '').slice(0, 8)}</div>
                      <div class="cell-mono">${String(item.uid || '').slice(0, 8)} · ${item.file || ''}</div>
                    </td>
                    <td class="cell-mono">${item.domain || '—'}</td>
                    <td>${item.expiresIn || '未知'}</td>
                    <td class="actions">
                      ${poolUids.has(item.uid)
                        ? html`<span class="badge badge-sm tone-muted">已导入</span>`
                        : html`<button class="btn btn-primary btn-xs" @click=${busyClick((e) => importOne(item, e.currentTarget))}>导入</button>`}
                    </td>
                  </tr>`)}
              </tbody>
            </table>
          </div>` : html`<div class="text-2xs muted">未检测到该版本的账号</div>`}
      </div>`;
  }

  function scanHtml() {
    const valid = detected.filter((item) => item.valid);
    const bad = detected.filter((item) => !item.valid);
    if (!detected.length) {
      return html`<p class="text-xs muted">没有在本机检测到桌面客户端的登录凭证。确认已经安装并登录客户端之后点「重新扫描」。</p>`;
    }
    return html`
      ${section('国际版 (www.workbuddy.ai)', valid.filter((item) => item.realm !== 'cn'))}
      ${section('国内版 (www.workbuddy.cn)', valid.filter((item) => item.realm === 'cn'))}
      ${bad.length ? html`
        <div class="scan-group">
          <div class="text-xs medium tone-warning">无法读取的凭证 · ${bad.length} 个</div>
          <ul class="preview-list">
            ${bad.map((item) => html`<li><code>${item.file}</code> — ${item.error || '未知原因'}</li>`)}
          </ul>
        </div>` : nothing}`;
  }

  async function scan() {
    busy = true;
    error = '';
    ctl.update();
    try {
      const response = await acct.scanDesktopCredentials();
      detected = response.detected || [];
      poolUids = new Set(response.pool_uids || []);
    } catch (err) {
      detected = null;
      error = '扫描失败：' + err.message;
    } finally {
      busy = false;
      ctl.update();
    }
  }

  async function importOne(item, button) {
    try {
      const response = await acct.importDesktopCredential(item.path, item.realm);
      const imported = (response.imported || [])[0] || {};
      const name = imported.nickname || String(imported.uid || '').slice(0, 8);
      toast.success('已导入 ' + name, imported.realm === 'cn' ? '归入国内版列表' : '归入国际版列表');
      acct.notifyAccountsChanged();
      await scan();
      if (imported.realm && imported.realm !== getState().viewRealm) {
        toast.info('这个账号属于' + (imported.realm === 'cn' ? '国内版' : '国际版') + '列表', '切换右上角「查看的版本」即可看到它');
      }
    } catch (err) {
      toastError('导入失败', err);
      button.disabled = false;
    }
  }

  scan();
  return ctl;
}
