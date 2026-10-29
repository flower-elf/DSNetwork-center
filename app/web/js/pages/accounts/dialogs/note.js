// 账号备注弹窗：只保存在本机账号文件里，留空即清除。
import { html } from '../../../core/dom.js';
import { icon } from '../../../core/icons.js';
import { openDialog } from '../../../ui/dialog.js';
import { busyClick } from '../../../ui/widgets.js';
import { toast, toastError } from '../../../ui/toast.js';
import * as acct from '../actions.js';

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
