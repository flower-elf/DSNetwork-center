// 设置 → 面板密码：卡片只显示当前状态，修改在独立弹窗里完成。
import { html } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { postJSON, setPanelToken } from '../../core/api.js';
import { openDialog } from '../../ui/dialog.js';
import { toast, toastError } from '../../ui/toast.js';
import { validatePasswordChange } from './logic.js';

export function createSecurityTab(page) {
  function openPasswordDialog() {
    const form = { current: '', next: '' };
    let error = '';
    let busy = false;

    const ctl = openDialog({
      width: 440,
      icon: 'LockKeyhole',
      title: '修改面板密码',
      desc: '用于打开本看板，与 API Key 相互独立；新密码至少 4 位。',
      body: () => html`
        <label class="field">
          <span class="field-label">当前密码</span>
          <input class="input" type="password" autocomplete="current-password" placeholder="当前密码"
                 .value=${form.current} ?disabled=${busy}
                 @input=${(e) => { form.current = e.target.value; error = ''; }}>
        </label>
        <label class="field">
          <span class="field-label">新密码</span>
          <input class="input" type="password" autocomplete="new-password" placeholder="至少 4 位"
                 .value=${form.next} ?disabled=${busy}
                 @input=${(e) => { form.next = e.target.value; error = ''; }}>
        </label>
        ${error ? html`<p class="field-error" role="alert">${error}</p>` : ''}`,
      foot: () => html`
        <button class="btn btn-ghost" ?disabled=${busy} @click=${() => ctl.close()}>取消</button>
        <button class="btn btn-primary" ?disabled=${busy} @click=${save}>
          ${busy ? icon('LoaderCircle', 'spin') : icon('LockKeyhole')}修改密码
        </button>`,
    });

    async function save() {
      error = validatePasswordChange(form.current, form.next);
      if (error) {
        ctl.update();
        return;
      }
      busy = true;
      ctl.update();
      try {
        const data = await postJSON('/panel/password', { current: form.current, new: form.next });
        if (data.token) setPanelToken(data.token);
        ctl.close();
        toast.success('面板密码已更新');
        await page.reloadSettings();
      } catch (err) {
        busy = false;
        // 输错当前密码属于填写错误，就地提示；其它失败交给全局提示。
        if (err.status === 400 || err.status === 401) error = '当前密码不正确，请重新输入。';
        else toastError('修改失败', err);
        ctl.update();
      }
    }
  }

  return {
    render() {
      const view = page.state.view || {};
      const isDefault = view.panel_password_is_default === true;
      return html`
        <div class="card">
          <div class="card-head">
            <h2 class="card-title">${icon('ShieldCheck')}面板密码</h2>
            <span class="card-sub">${isDefault ? '当前是默认密码 admin，建议立即修改' : '已改为自定义密码'}</span>
          </div>
          <div class="setting-list">
            <div class="setting">
              <div class="setting-main">
                <div class="setting-name">修改面板密码</div>
                <div class="setting-desc">打开本看板时输入的密码，与 API Key 相互独立。</div>
              </div>
              <div class="setting-control">
                <button class="btn btn-outline btn-sm" @click=${openPasswordDialog}>
                  ${icon('LockKeyhole')}修改密码
                </button>
              </div>
            </div>
          </div>
        </div>`;
    },
  };
}
