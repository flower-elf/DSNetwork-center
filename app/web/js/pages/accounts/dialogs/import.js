// 导入账号弹窗：先预览服务端的校验结果，确认后再写盘。
import { html, nothing } from '../../../core/dom.js';
import { icon } from '../../../core/icons.js';
import { confirmDialog, openDialog } from '../../../ui/dialog.js';
import { busyClick, notice } from '../../../ui/widgets.js';
import { toast, toastError } from '../../../ui/toast.js';
import * as acct from '../actions.js';

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
