// 设置 → 费用估算：每个模型的官方价格（元 / 百万 Token）与积分折算单价；控件先改本地草稿，由卡片上的「保存」统一提交。
import { html, live, nothing } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { toast, toastError } from '../../ui/toast.js';
import { COST_PRICE_FIELDS, costEstimateDirty, costEstimatePayload, costEstimateState } from './logic.js';
import { numberRow, settingCard } from './rows.js';

export function createCostTab(page) {
  const form = { creditPrice: '0', rows: [] };
  const errors = { creditPrice: '', rows: [] };
  let baseline = null;
  let saving = false;
  let dirtyShown = false;

  function stateOf(view) {
    return costEstimateState((view || {}).cost_estimate);
  }

  function applyState(state) {
    form.creditPrice = state.creditPrice;
    form.rows = state.rows.map((row) => ({ ...row }));
    errors.creditPrice = '';
    errors.rows = [];
    dirtyShown = false;
  }

  function seed(view) {
    baseline = stateOf(view);
    applyState(baseline);
  }

  function isDirty() {
    return costEstimateDirty(form, baseline);
  }

  function markDirty() {
    if (isDirty() === dirtyShown) return;
    dirtyShown = isDirty();
    page.draw();
  }

  function structChanged() {
    dirtyShown = isDirty();
    page.draw();
  }

  function revert() {
    if (!baseline) return;
    applyState(baseline);
    page.draw();
    toast.info('已撤销未保存的改动');
  }

  function addRow() {
    form.rows.push({ model: '', input: '0', cached_input: '0', output: '0' });
    structChanged();
  }

  function removeRow(index) {
    if (index < 0 || index >= form.rows.length) return;
    form.rows.splice(index, 1);
    errors.rows.splice(index, 1);
    structChanged();
  }

  async function saveAll() {
    const built = costEstimatePayload(form);
    errors.creditPrice = built.errors.creditPrice;
    errors.rows = built.errors.rows;
    if (!built.ok) {
      page.draw();
      return false;
    }
    saving = true;
    page.draw();
    try {
      const view = await page.save(built.payload);
      baseline = stateOf(view);
      applyState(baseline);
      toast.success('费用估算设置已保存');
      return true;
    } catch (err) {
      toastError('保存费用估算设置失败', err);
      return false;
    } finally {
      saving = false;
      page.draw();
    }
  }

  // 字段行共用的表单状态：模型价格表的每一行另有自己的错误位，不走这里。
  const ctl = {
    form,
    get saving() { return saving; },
    errors,
    change: (field, value) => {
      form[field] = value;
      if (field in errors) errors[field] = '';
      markDirty();
    },
  };

  function modelTable() {
    return html`
      <div class="table-wrap">
        <table class="table table-compact">
          <thead>
            <tr>
              <th>模型</th>
              ${COST_PRICE_FIELDS.map((field) => html`<th class="right">${field.label}</th>`)}
              <th></th>
            </tr>
          </thead>
          <tbody>
            ${form.rows.map((row, index) => html`
              <tr>
                <td>
                  <input class="input input-sm" type="text" placeholder="例如 deepseek-v4.1-flash"
                         aria-label="模型名称"
                         .value=${live(row.model)} ?disabled=${saving}
                         @input=${(e) => { row.model = e.target.value; errors.rows[index] = ''; markDirty(); }}>
                  ${errors.rows[index] ? html`<div class="field-error">${errors.rows[index]}</div>` : nothing}
                </td>
                ${COST_PRICE_FIELDS.map((field) => html`
                  <td class="right">
                    <input class="input input-sm input-num" type="number" min="0" step="0.0001"
                           aria-label=${field.label + ' ' + (row.model || '第 ' + (index + 1) + ' 行')}
                           .value=${live(row[field.key])} ?disabled=${saving}
                           @input=${(e) => { row[field.key] = e.target.value; errors.rows[index] = ''; markDirty(); }}>
                  </td>`)}
                <td class="right">
                  <button class="btn btn-ghost btn-sm" ?disabled=${saving} aria-label="删除这一行"
                          @click=${() => removeRow(index)}>${icon('Trash2')}</button>
                </td>
              </tr>`)}
            ${form.rows.length === 0 ? html`
              <tr><td colspan="5" class="text-xs muted">还没有配置模型价格。没有价格的模型不参与费用估算。</td></tr>` : nothing}
          </tbody>
        </table>
      </div>`;
  }

  return {
    seed,
    render() {
      return settingCard({
        iconName: 'Calculator',
        title: '费用估算',
        desc: '官方价格按每百万 Token 填写，用于计算「如果直接调用官方 API 要花多少钱」；默认内置 deepseek-v4.1-flash 的官方高峰时段价格。未配置价格的模型按 0 计，不参与估算。',
        dirty: isDirty(),
        saving,
        onRevert: revert,
        onSave: saveAll,
        body: html`
          <div class="setting-list">
            ${numberRow(ctl, {
              name: '积分折算单价',
              field: 'creditPrice',
              suffix: '元 / 积分',
              step: '0.0001',
              desc: '1 积分折合多少元，用来把消耗的积分换算成支出。填写 0 表示积分不计支出，省了多少等于官方价格合计。',
            })}
          </div>
          <div class="card-head">
            <div>
              <h3 class="card-title">模型官方价格</h3>
              <div class="card-sub">单位为元 / 百万 Token；缓存命中价格留空时按输入价格计费。</div>
            </div>
            <button class="btn btn-outline btn-sm" ?disabled=${saving} @click=${addRow}>
              ${icon('Plus')}添加模型
            </button>
          </div>
          ${modelTable()}
          <p class="field-hint">
            内置价格只有 deepseek-v4.1-flash，其余模型需要在这里填写后才会进入费用估算。
          </p>`,
      });
    },
    /** 有未保存的改动时先问一句：保存、放弃或取消这次跳转。 */
    async canLeave() {
      if (!isDirty()) return true;
      const choice = await page.confirmUnsaved({
        desc: '「费用估算」里的改动还没有保存。',
        onSave: saveAll,
      });
      return choice !== 'cancel';
    },
  };
}
