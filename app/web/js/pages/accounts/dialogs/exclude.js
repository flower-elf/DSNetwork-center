// 模型排除弹窗：勾选这个账号不接的模型，保存成账号的排除名单。
import { html, nothing } from '../../../core/dom.js';
import { icon } from '../../../core/icons.js';
import { openDialog } from '../../../ui/dialog.js';
import { busyClick, notice } from '../../../ui/widgets.js';
import { toast, toastError } from '../../../ui/toast.js';
import * as acct from '../actions.js';
import { modelSelection, deniedModels } from '../logic.js';

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
