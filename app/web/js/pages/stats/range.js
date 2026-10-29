// 用量与费用估算共用的时间范围控件：范围分段、版本合并开关与自定义起止时间。
import { html } from '../../core/dom.js';
import { getState } from '../../core/store.js';
import { toLocalInputValue, fromLocalInputValue } from '../../core/format.js';
import { segmented, chip } from '../../ui/widgets.js';
import { RANGES, scopeRealm, scopeText, buildQuery } from './logic.js';

/**
 * redraw() 只重绘页面，reload() 重新取数。query() 返回当前范围的查询串；
 * actions() 是页头的操作区，customCard() 是自定义起止时间的卡片。
 */
export function createRangeControls({ redraw, reload }) {
  let range = 'today';
  let sinceInput = '';
  let untilInput = '';
  let includeAll = false;

  /** 自定义区间第一次打开时把起点填成今天零点：空输入等于不限，画面会和「全部」一模一样，看起来像按钮坏了。终点留空表示到现在。 */
  function prefillCustomRange() {
    if (sinceInput) return;
    const d = new Date();
    sinceInput = toLocalInputValue(new Date(d.getFullYear(), d.getMonth(), d.getDate()).getTime() / 1000);
  }

  function selectRange(id) {
    if (range === id) return;
    range = id;
    if (id === 'custom') prefillCustomRange();
    redraw();
    reload();
  }

  function toggleScope() {
    includeAll = !includeAll;
    redraw();
    reload();
  }

  return {
    get range() { return range; },
    get includeAll() { return includeAll; },
    /** 当前统计的版本合并口径，供「本月积分」这类只按版本取数的接口拼查询串。 */
    realm() {
      return scopeRealm(getState().viewRealm, includeAll);
    },
    query() {
      return buildQuery({
        realm: scopeRealm(getState().viewRealm, includeAll),
        range,
        since: fromLocalInputValue(sinceInput),
        until: fromLocalInputValue(untilInput),
      });
    },
    actions() {
      return html`
        ${segmented(RANGES, range, selectRange)}
        ${chip(includeAll ? '含两个版本' : '仅' + scopeText(false, getState().viewRealm), includeAll, toggleScope)}`;
    },
    customCard() {
      return html`
        <div class="card card-tight">
          <div class="row-wrap">
            <label class="row text-2xs muted">起点
              <input class="input input-sm" type="datetime-local" .value=${sinceInput}
                     @change=${(e) => { sinceInput = e.target.value; reload(); }}>
            </label>
            <label class="row text-2xs muted">终点
              <input class="input input-sm" type="datetime-local" .value=${untilInput}
                     @change=${(e) => { untilInput = e.target.value; reload(); }}>
            </label>
            <span class="text-2xs muted">终点留空表示统计到现在。</span>
          </div>
        </div>`;
    },
  };
}
