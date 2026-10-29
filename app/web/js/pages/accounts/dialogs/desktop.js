// 桌面客户端账号扫描弹窗。
import { html, nothing } from '../../../core/dom.js';
import { icon } from '../../../core/icons.js';
import { getState } from '../../../core/store.js';
import { openDialog } from '../../../ui/dialog.js';
import { busyClick, notice } from '../../../ui/widgets.js';
import { toast, toastError } from '../../../ui/toast.js';
import * as acct from '../actions.js';

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
