// 设置 → 代理槽：槽位增删、测试连通性、自动发现并导入、批量分配给未绑定账号。
import { html, nothing } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { getJSON, postJSON } from '../../core/api.js';
import { confirmDialog } from '../../ui/dialog.js';
import { toast, toastError } from '../../ui/toast.js';
import { addSlot, autoAssignPlan, bindingText, discoverText, importCandidates, removeSlot, slotPayload, slotsDirty, unboundEnabledAccounts, validateSlots } from './logic.js';

// 编辑器状态放在模块级：按地址切换标签时设置页会整页卸载重挂，正在填的行不能因此丢掉。
let slots = [];
// 服务端最后给的列表：有没有未保存的改动由它和当前列表比出来。
let baseline = [];
let discover = [];
let picked = new Set();
let fetched = false;

export function createProxyTab(page) {
  let shownDirty = slotsDirty(slots, baseline);
  let busy = '';

  const isDirty = () => slotsDirty(slots, baseline);

  /** 服务端列表整份替换编辑器内容：未保存状态随之复位。 */
  function adopt(list) {
    slots = (list || []).map((slot) => ({ ...slot }));
    baseline = slots.map((slot) => ({ ...slot }));
    shownDirty = false;
  }

  function markDirty() {
    const next = isDirty();
    if (next !== shownDirty) {
      // 表头的「未保存」翻转时才重绘，输入过程中不打断。
      shownDirty = next;
      page.draw();
    }
  }

  /** 行数变化后必须重绘。 */
  function structChanged() {
    shownDirty = isDirty();
    page.draw();
  }

  /** 读取槽位；有未保存的改动时不覆盖编辑器内容（问题 #79）。 */
  async function loadSlots() {
    if (isDirty()) return;
    try {
      const data = await getJSON('/proxy/slots');
      adopt(data.slots);
      fetched = true;
      page.draw();
    } catch (err) {
      toastError('读取代理槽失败', err);
    }
  }

  async function saveSlots() {
    const problem = validateSlots(slots);
    if (problem) {
      toast.warn(problem);
      return;
    }
    busy = 'save';
    page.draw();
    try {
      const data = await postJSON('/proxy/slots/save', { slots: slotPayload(slots) });
      adopt(data.slots);
      toast.success(`已保存 ${slots.length} 个代理槽`);
    } catch (err) {
      toastError('保存代理槽失败', err);
    } finally {
      busy = '';
      page.draw();
    }
  }

  async function testSlot(index) {
    const slot = slots[index];
    if (!slot) return;
    if (!String(slot.url || '').trim()) {
      toast.warn('这个槽位还没有填写代理地址');
      return;
    }
    busy = 'test' + index;
    page.draw();
    try {
      let id = slot.id;
      if (!id) {
        // 新槽位服务端还不知道，先整份保存拿到 id 再测试。
        const problem = validateSlots(slots);
        if (problem) {
          toast.warn(problem);
          return;
        }
        const saved = await postJSON('/proxy/slots/save', { slots: slotPayload(slots) });
        adopt(saved.slots);
        id = (slots[index] || {}).id;
      }
      const result = await postJSON('/proxy/slots/test', { id });
      if (result.ok) toast.success(`出口 IP ${result.exit_ip}，延迟 ${result.latency_ms} ms`);
      else toast.error('测试失败', result.error || '未知原因');
    } catch (err) {
      toastError('测试失败', err);
    } finally {
      busy = '';
      page.draw();
    }
  }

  async function discoverSlots() {
    busy = 'discover';
    page.draw();
    try {
      const data = await postJSON('/proxy/discover', {});
      discover = data.candidates || [];
      picked = new Set(discover.filter((c) => c.reachable).map((c) => c.url));
      toast.info(discoverText(discover));
    } catch (err) {
      toastError('自动发现失败', err);
    } finally {
      busy = '';
      page.draw();
    }
  }

  function importPicked() {
    const before = slots.length;
    slots = importCandidates(slots, discover, [...picked]);
    const added = slots.length - before;
    if (!added) {
      toast.warn('勾选的出口都已经在列表里');
      return;
    }
    structChanged();
    toast.info(`已加入 ${added} 个槽位，点「保存槽位」后生效`);
  }

  async function autoAssign() {
    if (isDirty()) {
      toast.warn('代理槽有未保存的改动，请先保存再自动分配');
      return;
    }
    busy = 'assign';
    page.draw();
    try {
      const data = await getJSON('/accounts?realm=all');
      const accounts = data.accounts || [];
      const unbound = unboundEnabledAccounts(accounts);
      if (!unbound.length) {
        toast.info('所有已启用账号都已绑定代理出口');
        return;
      }
      const plan = autoAssignPlan(accounts, slots);
      if (!plan.length) {
        toast.warn('没有启用中的代理槽，请先添加并保存');
        return;
      }
      busy = '';
      page.draw();
      await confirmDialog({
        title: '自动分配代理出口？',
        desc: `将为 ${plan.length} 个还没有绑定出口的启用账号分配代理槽，已经绑定过的账号保持不变。`,
        confirmText: '开始分配',
        onConfirm: async () => {
          for (const item of plan) {
            await postJSON('/accounts/set', { uid: item.uid, proxySlot: item.slotId });
          }
          await loadSlots();
          toast.success(`已为 ${plan.length} 个账号分配代理出口`);
        },
      });
    } catch (err) {
      toastError('自动分配失败', err);
    } finally {
      busy = '';
      page.draw();
    }
  }

  function removeRow(index) {
    const slot = slots[index];
    if (!slot) return;
    const bound = Number(slot.bound) || 0;
    const apply = () => {
      slots = removeSlot(slots, index);
      structChanged();
      toast.info('已从列表移除，点「保存槽位」后生效');
    };
    if (!bound) {
      apply();
      return;
    }
    confirmDialog({
      title: '删除这个槽位？',
      desc: `该槽位被 ${bound} 个启用中的账号绑定，保存后会解除绑定并回退直连。`,
      confirmText: '删除',
      danger: true,
      onConfirm: async () => apply(),
    });
  }

  function slotRow(slot, index) {
    const testing = busy === 'test' + index;
    return html`
      <tr>
        <td>
          <input class="input input-sm" type="text" placeholder="例如：香港 01"
                 .value=${slot.name || ''} @input=${(e) => { slots[index].name = e.target.value; markDirty(); }}>
        </td>
        <td>
          <input class="input input-sm mono" type="text" placeholder="http://127.0.0.1:7890"
                 .value=${slot.url || ''} @input=${(e) => { slots[index].url = e.target.value; markDirty(); }}>
        </td>
        <td>
          <label class="switch">
            <input type="checkbox" .checked=${slot.enabled !== false} aria-label="启用这个槽位"
                   @change=${(e) => { slots[index].enabled = e.target.checked; markDirty(); }}>
            <span></span>
          </label>
        </td>
        <td><span class="text-xs muted">${bindingText(slot.bound)}</span></td>
        <td class="actions">
          <button class="btn btn-outline btn-xs" ?disabled=${testing} @click=${() => testSlot(index)}>
            ${testing ? icon('LoaderCircle', 'spin') : nothing}测试
          </button>
          <button class="btn btn-ghost btn-icon btn-xs tone-danger" title="删除槽位" @click=${() => removeRow(index)}>${icon('Trash2')}</button>
        </td>
      </tr>`;
  }

  return {
    render() {
      return html`
        <div class="card">
          <div class="card-head">
            <div>
              <h2 class="card-title">${icon('Network')}代理槽</h2>
              <div class="card-sub">
                每个账号固定绑定一个代理槽：槽位地址变化时绑定账号自动跟随；槽位停用后绑定账号回退直连，重新启用即恢复；槽位删除会解除绑定并回退直连。
                <span class="text-2xs ${isDirty() ? 'tone-warning' : 'muted'}">${isDirty() ? '(代理槽有未保存的改动)' : (fetched ? `共 ${slots.length} 个槽位` : '(正在读取代理槽)')}</span>
              </div>
            </div>
            <div class="row-wrap">
              <button class="btn btn-outline btn-sm" ?disabled=${busy === 'discover'} @click=${discoverSlots}>
                ${busy === 'discover' ? icon('LoaderCircle', 'spin') : icon('Search')}自动发现 mihomo 端口
              </button>
              <button class="btn btn-outline btn-sm" @click=${() => { slots = addSlot(slots); structChanged(); }}>
                ${icon('Plus')}添加槽位
              </button>
              <button class="btn btn-outline btn-sm" ?disabled=${busy === 'assign'} @click=${autoAssign}>${icon('Wand2')}自动分配</button>
              <button class="btn btn-primary btn-sm" ?disabled=${busy === 'save'} @click=${saveSlots}>
                ${busy === 'save' ? icon('LoaderCircle', 'spin') : icon('Save')}保存槽位
              </button>
            </div>
          </div>
          ${slots.length ? html`
            <div class="table-wrap">
              <table class="table slot-table">
                <thead>
                  <tr><th>名称</th><th>代理地址</th><th>启用</th><th>绑定账号</th><th class="actions">操作</th></tr>
                </thead>
                <tbody>${slots.map((slot, index) => slotRow(slot, index))}</tbody>
              </table>
            </div>` : html`
            <p class="text-xs muted">还没有代理槽。点「自动发现 mihomo 端口」或「添加槽位」创建。</p>`}
          ${discover.length ? html`
            <div class="discover-box">
              <div class="text-xs muted">勾选要导入为槽位的出口。不可达的地址已禁用。</div>
              ${discover.map((candidate) => html`
                <label class="row text-xs">
                  <input type="checkbox" .checked=${picked.has(candidate.url)} ?disabled=${!candidate.reachable}
                         @change=${(e) => { if (e.target.checked) picked.add(candidate.url); else picked.delete(candidate.url); }}>
                  <code class="mono">${candidate.url}</code>
                  <span class="${candidate.reachable ? 'tone-success' : 'tone-danger'}">
                    ${candidate.reachable ? `出口 ${candidate.exit_ip} · ${candidate.latency_ms}ms` : '不可达'}
                  </span>
                </label>`)}
              <button class="btn btn-outline btn-sm" @click=${importPicked}>${icon('Download')}导入勾选的出口</button>
            </div>` : nothing}
          <p class="text-2xs muted">「绑定账号」只统计启用中的账号；在账号页的出口下拉里调整绑定。测试会真实经过代理访问一次外网。</p>
        </div>`;
    },
    onMount() {
      loadSlots();
    },
  };
}
