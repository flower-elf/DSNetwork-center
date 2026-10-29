// 设置 → 运维里的更新检查块：读取更新状态、「立即检查」与发布页链接。
import { html, nothing } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { getJSON, postJSON } from '../../core/api.js';
import { releaseUrl, updateAvailable } from '../../core/update.js';
import { openUpdateDialog } from '../../ui/update.js';
import { toast, toastError } from '../../ui/toast.js';
import { updateStatusHint } from './logic.js';

export function createUpdateBlock(page) {
  let updateStatus = null;
  let checkingUpdate = false;

  async function load() {
    try {
      updateStatus = await getJSON('/updates');
    } catch (err) {
      updateStatus = { error: err && err.message ? err.message : String(err) };
    }
    page.draw();
  }

  async function checkNow() {
    if (checkingUpdate) return;
    checkingUpdate = true;
    page.draw();
    try {
      const status = await postJSON('/updates/check', {});
      updateStatus = status;
      if (status.ok === false) toast.warn('检查更新失败', status.msg || '更新检查未运行');
      else if (updateAvailable(status)) openUpdateDialog(status);
      else if (status.last_error) toast.warn('检查更新失败', status.last_error);
      else if (status.latest_version) toast.success('已是最新版本', '当前 v' + (status.current_version || ''));
    } catch (err) {
      toastError('检查更新失败', err);
    } finally {
      checkingUpdate = false;
      page.draw();
    }
  }

  function render() {
    const hint = updateStatusHint(updateStatus);
    const url = releaseUrl(updateStatus || {});
    return html`
      <div class="setting">
        <div class="setting-main">
          <div class="setting-name">立即检查更新</div>
          <div class="setting-desc">现在向 GitHub 查询一次最新稳定版，不受自动检查间隔限制。</div>
          <div class="field-hint ${hint.warn ? 'tone-warning' : ''}">${hint.text}</div>
          ${url
            ? html`<div class="field-hint">
                <a href=${url} target="_blank" rel="noopener noreferrer">打开发布页</a>
              </div>`
            : nothing}
        </div>
        <div class="setting-control">
          <button class="btn btn-outline btn-sm" ?disabled=${checkingUpdate} @click=${checkNow}>
            ${checkingUpdate ? icon('LoaderCircle', 'spin') : icon('RefreshCw')}${checkingUpdate ? '检查中…' : '立即检查'}
          </button>
        </div>
      </div>`;
  }

  return { load, render };
}
