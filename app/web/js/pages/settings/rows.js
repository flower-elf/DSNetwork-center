// 设置页各卡片共用的外壳与字段行。ctl 提供表单状态与改动回调：
// { form, saving, errors?, env?(field), change(field, value) }；errors / env 缺省表示这一组不显示。
import { html, live, nothing } from '../../core/dom.js';
import { icon } from '../../core/icons.js';

/** 环境变量覆盖提示；没有覆盖时返回空。 */
export function envHint(env) {
  return env ? html`<div class="field-hint tone-warning">环境变量 ${env.name} = ${env.value}，优先于面板设置</div>` : nothing;
}

function rowError(ctl, field) {
  const message = ctl.errors ? ctl.errors[field] : '';
  return message ? html`<div class="field-error">${message}</div>` : nothing;
}

/** 设置卡片外壳：标题、说明、未保存标记与「撤销 / 保存」按钮；body 是卡片内容。 */
export function settingCard({ iconName, title, desc, dirty, saving, onRevert, onSave, body }) {
  return html`
    <div class="card">
      <div class="card-head">
        <div>
          <h2 class="card-title">${icon(iconName)}${title}</h2>
          <div class="card-sub">
            ${desc}
            <span class="text-2xs ${dirty ? 'tone-warning' : 'muted'}">${dirty ? '(有未保存的改动)' : ''}</span>
          </div>
        </div>
        <div class="row-wrap">
          <button class="btn btn-outline btn-sm" ?disabled=${saving || !dirty} @click=${onRevert}>
            ${icon('Undo2')}撤销
          </button>
          <button class="btn btn-primary btn-sm" ?disabled=${saving || !dirty} @click=${onSave}>
            ${saving ? icon('LoaderCircle', 'spin') : icon('Save')}保存
          </button>
        </div>
      </div>
      ${body}
    </div>`;
}

/** 开关行。 */
export function switchRow(ctl, { name, desc, field }) {
  return html`
    <div class="setting">
      <div class="setting-main">
        <div class="setting-name">${name}</div>
        <div class="setting-desc">${desc}</div>
        ${envHint(ctl.env && ctl.env(field))}
      </div>
      <div class="setting-control">
        <label class="switch">
          <input type="checkbox" .checked=${live(ctl.form[field])} ?disabled=${ctl.saving} aria-label=${name}
                 @change=${(e) => ctl.change(field, e.target.checked)}>
          <span></span>
        </label>
      </div>
    </div>`;
}

/** 数字输入行。 */
export function numberRow(ctl, { name, desc, field, suffix, min = '0', step = '1' }) {
  return html`
    <div class="setting">
      <div class="setting-main">
        <div class="setting-name">${name}</div>
        <div class="setting-desc">${desc}</div>
        ${envHint(ctl.env && ctl.env(field))}
        ${rowError(ctl, field)}
      </div>
      <div class="setting-control">
        <input class="input input-num" type="number" min=${min} step=${step} aria-label=${name}
               .value=${live(ctl.form[field])} ?disabled=${ctl.saving}
               @input=${(e) => ctl.change(field, e.target.value)}>
        <span class="setting-unit">${suffix}</span>
      </div>
    </div>`;
}

/** 文本输入行。 */
export function textRow(ctl, { name, desc, field, placeholder = '' }) {
  return html`
    <div class="setting">
      <div class="setting-main">
        <div class="setting-name">${name}</div>
        <div class="setting-desc">${desc}</div>
        ${envHint(ctl.env && ctl.env(field))}
        ${rowError(ctl, field)}
      </div>
      <div class="setting-control">
        <input class="input input-sm select-wide" type="text" placeholder=${placeholder}
               aria-label=${name}
               .value=${live(ctl.form[field])} ?disabled=${ctl.saving}
               @input=${(e) => ctl.change(field, e.target.value)}>
      </div>
    </div>`;
}
