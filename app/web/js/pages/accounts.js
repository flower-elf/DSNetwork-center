// 账号页：状态、数据读取与行内操作。模板在 accounts/view.js，批量流程在 accounts/flows.js，
// 筛选、排序、分页与可用性分档在 accounts/logic.js，这里只负责把它们接起来。
import { render } from '../core/dom.js';
import { getState } from '../core/store.js';
import { realmLabel } from '../core/format.js';
import { confirmDialog } from '../ui/dialog.js';
import { toast, toastError } from '../ui/toast.js';
import * as logic from './accounts/logic.js';
import * as activity from './accounts/activity.js';
import * as acct from './accounts/actions.js';
import * as flows from './accounts/flows.js';
import { accountView } from './accounts/view.js';

export const autoRefresh = true;

export function mount(host) {
  const S = {
    accounts: [],
    usage: {},
    slots: [],
    activity: {},
    standDown: null,
    limits: null,
    query: '',
    filter: 'all',
    sort: 'default',
    page: 1,
    loading: true,
    error: '',
    partial: '',
    draft: null,
    tried: new Set(),
    showTech: false,
    menuOpen: false,
  };

  // 心跳会整块重绘，正在编辑的输入框先把内容与光标位置记下来，渲染后写回。
  function captureDraft() {
    const el = document.activeElement;
    if (!el || !el.dataset || !el.dataset.draft) {
      S.draft = null;
      return;
    }
    S.draft = {
      uid: el.dataset.uid || '',
      field: el.dataset.draft,
      value: el.value,
      start: el.selectionStart,
      end: el.selectionEnd,
    };
  }

  function restoreDraft() {
    const draft = S.draft;
    S.draft = null;
    if (!draft) return;
    const el = host.querySelector('input[data-draft="' + draft.field + '"][data-uid="' + draft.uid + '"]');
    if (!el) return;
    el.focus();
    // 数字输入框不支持 setSelectionRange，只有文本输入框需要恢复光标位置。
    if (el.type !== 'number' && el.setSelectionRange && typeof draft.start === 'number') {
      el.setSelectionRange(draft.start, draft.end);
    }
  }

  function draw() {
    captureDraft();
    render(view(), host);
    restoreDraft();
  }

  // ---------------------------------------------------------------- 数据

  let loadSeq = 0;

  async function refresh() {
    const mine = ++loadSeq;
    // 账号列表与三份辅助数据互不依赖，一起发出。
    const [accountsResult, auxResult] = await Promise.allSettled([
      acct.loadAccounts(),
      Promise.all([acct.loadUsageByAccount(), acct.loadProxySlots(), acct.loadActivityToday()]),
    ]);
    if (mine !== loadSeq) return;
    S.loading = false;
    if (accountsResult.status === 'rejected') {
      const err = accountsResult.reason || {};
      if (!S.accounts.length) {
        S.error = err.message || '读取账号失败';
        draw();
        return;
      }
      S.partial = '账号列表这次没有读取成功，下面显示的是上一次的数据：' + (err.message || err);
    } else {
      const payload = accountsResult.value || {};
      S.accounts = payload.accounts || [];
      S.standDown = payload.stand_down || null;
      S.limits = payload.limits || null;
      S.error = '';
    }
    if (auxResult.status === 'fulfilled') {
      const [usage, slots, today] = auxResult.value;
      S.usage = {};
      for (const row of usage.accounts || []) S.usage[row.account] = row;
      S.slots = slots.slots || [];
      S.activity = activity.latestByUid(today.rows || []);
      if (accountsResult.status === 'fulfilled') S.partial = '';
    } else {
      S.partial = '用量、代理槽或今日活动这次没有读取成功，相关列可能是旧的：'
        + (auxResult.reason && auxResult.reason.message ? auxResult.reason.message : auxResult.reason);
    }
    draw();
    if (accountsResult.status === 'fulfilled') fillMissingCredits();
  }

  // 刚导入的账号补查一次积分；每个账号在一次页面生命周期里只查一次，避免每轮心跳重复请求。
  async function fillMissingCredits() {
    const todo = logic.creditsToFill(S.accounts, S.tried);
    if (!todo.length) return;
    const mine = loadSeq;
    let started = 0;
    let touched = false;
    try {
      for (let index = 0; index < todo.length; index += 1) {
        if (mine !== loadSeq) break;
        const uid = todo[index];
        // 只标记已经发起请求的账号；中途被新的刷新取代时，后面没查的账号
        // 才能在 finally 里放回待查集合。
        S.tried.add(uid);
        started = index + 1;
        const result = await acct.fetchCredits(uid);
        if (mine !== loadSeq) break;
        if (result.accounts) {
          S.accounts = result.accounts;
          touched = true;
        } else if (result.results) {
          S.accounts = logic.mergeCredits(S.accounts, result.results);
          touched = true;
        }
      }
      if (mine === loadSeq && touched) draw();
    } catch (err) {
      if (mine !== loadSeq) return;
      S.partial = '有账号的积分没有查询成功：' + err.message;
      draw();
    } finally {
      if (mine !== loadSeq) {
        for (let index = started; index < todo.length; index += 1) S.tried.delete(todo[index]);
      }
    }
  }

  function applyAccountPayload(payload) {
    if (payload && payload.accounts) {
      loadSeq += 1;
      S.accounts = payload.accounts;
    }
  }

  function applyAndDraw(result) {
    applyAccountPayload(result);
    draw();
  }

  // ---------------------------------------------------------------- 动作

  async function refreshCredits() {
    const realm = getState().viewRealm;
    try {
      const result = await acct.refreshCredits(realm);
      applyAccountPayload(result);
      draw();
      toast.success('已刷新' + realmLabel(realm) + ' ' + ((result.results || []).length) + ' 个账号的积分');
    } catch (err) {
      toastError('刷新积分失败', err);
    }
  }

  const doCheckin = () => flows.runCheckin({ apply: applyAndDraw });

  const doDailyChatPending = () => flows.runDailyChatPending({
    accounts: S.accounts,
    byUid: S.activity,
    apply: async (result) => {
      applyAccountPayload(result);
      await refresh();
    },
  });

  const doDailyChatWeb = () => flows.runDailyChatWeb({ apply: applyAndDraw });

  async function exportAll() {
    try {
      const name = await acct.exportAccounts();
      toast.success('账号已导出', name);
    } catch (err) {
      toastError('导出失败', err);
    }
  }

  async function exportOne(account) {
    try {
      const name = await acct.exportAccounts(account.uid);
      toast.success('账号已导出', name);
    } catch (err) {
      toastError('导出失败', err);
    }
  }

  async function assignSlots() {
    const plan = logic.autoAssignPlan(S.accounts, S.slots);
    if (!plan.rows.length) {
      toast.warn(plan.instead);
      return;
    }
    let done = 0;
    for (const row of plan.rows) {
      try {
        await acct.setProxySlot(row.uid, row.slotId);
        done += 1;
      } catch (err) {
        toastError('分配代理出口失败', err);
        break;
      }
    }
    await refresh();
    if (done) toast.success('已为 ' + done + ' 个未绑定账号分配代理出口');
  }

  async function setAll(enabled) {
    if (!enabled) {
      const go = await confirmDialog({
        title: '停用全部账号？',
        desc: logic.disableAllText(),
        confirmText: '全部停用',
        danger: true,
      });
      if (!go) return;
    }
    try {
      await acct.setAllEnabled(enabled);
      toast.success(enabled ? '已启用全部账号' : '已停用全部账号');
      await refresh();
    } catch (err) {
      toastError('批量操作失败', err);
    }
  }

  async function toggleAccount(account, enabled) {
    try {
      await acct.setEnabled(account.uid, enabled);
      toast.success(enabled ? '已启用账号 ' + account.nickname : '已停用账号 ' + account.nickname);
      await refresh();
    } catch (err) {
      toastError('操作失败', err);
    }
  }

  async function removeAccount(account) {
    const name = account.nickname || String(account.uid || '').slice(0, 8);
    const go = await confirmDialog({
      title: '删除账号「' + name + '」？',
      desc: '删除会移除本机保存的这份凭证，这个账号之后不再参与任何请求，需要重新添加或重新导入才能恢复。如果只是想暂时不用它，点「停用」更稳妥。',
      confirmText: '删除账号',
      danger: true,
      onConfirm: async () => {
        const result = await acct.deleteAccount(account.uid);
        applyAccountPayload(result);
        draw();
      },
    });
    if (go) toast.success('已删除账号 ' + name);
  }

  async function testAccount(account) {
    try {
      const result = await acct.testAccount(account.uid);
      if (result.ok) {
        toast.success('测试通过，用时 ' + result.elapsed_ms + ' ms', '模型 ' + result.model + '：' + (result.reply || '正常'));
      } else {
        toast.warn('测试失败', (result.model ? result.model + '：' : '') + (result.error || result.status || '异常'));
      }
      await refresh();
    } catch (err) {
      toastError('测试请求失败', err);
    }
  }

  async function refreshToken(account) {
    try {
      const result = await acct.refreshAccountToken(account.uid);
      const one = (result.results || [])[0];
      if (one && one.ok) toast.success('账号凭证刷新成功');
      else toast.warn('刷新失败', (one && one.error) || '上游没有返回结果');
      await refresh();
    } catch (err) {
      toastError('刷新失败', err);
    }
  }

  async function onSlotChange(account, slotId) {
    // 旧代理只是如实展示，选中它不提交任何改动。
    if (slotId === '__legacy__') return;
    try {
      await acct.setProxySlot(account.uid, slotId);
      toast.success(slotId ? '出口已更新' : '出口已改为直连');
      await refresh();
    } catch (err) {
      toastError('出口更新失败', err);
      draw();
    }
  }

  async function onProductChange(account, productId) {
    const current = logic.productOptions(account).selected;
    if (productId === current) return;
    const go = await confirmDialog({
      title: '切换出站身份？',
      desc: '把这个账号的出站身份切换为「' + logic.productLabel(productId) + '」。三种身份对应不同的出站指纹、配额通道与限流口径，切换需要在途请求结束后才生效。',
      confirmText: '切换身份',
    });
    if (!go) {
      draw();
      return;
    }
    try {
      await acct.setProduct(account.uid, productId);
      toast.success('已切换为 ' + logic.productLabel(productId));
      await refresh();
    } catch (err) {
      toastError('切换失败', err);
      draw();
    }
  }

  function onNumberChange(raw, { parse, current, commit }) {
    const parsed = parse(raw);
    if (!parsed.ok) {
      toast.warn(parsed.message);
      draw();
      return;
    }
    if (parsed.value === current) {
      draw();
      return;
    }
    commit(parsed.value);
  }

  function onPriorityChange(account, raw) {
    onNumberChange(raw, {
      parse: (value) => logic.parsePriorityInput(value, S.limits),
      current: logic.priorityOf(account),
      commit: (value) => setPriority(account.uid, value),
    });
  }

  async function setPriority(uid, priority) {
    try {
      await acct.setPriority(uid, priority);
      toast.success('优先级已更新为 ' + priority);
      await refresh();
    } catch (err) {
      toastError('优先级更新失败', err);
      draw();
    }
  }

  function onConcurrencyChange(account, raw) {
    onNumberChange(raw, {
      parse: (value) => logic.parseConcurrencyInput(value, S.limits),
      current: logic.concurrencyOf(account),
      commit: (value) => setConcurrencyLimit(account.uid, value),
    });
  }

  async function setConcurrencyLimit(uid, concurrencyLimit) {
    try {
      await acct.setConcurrencyLimit(uid, concurrencyLimit);
      toast.success('并发上限已更新');
      await refresh();
    } catch (err) {
      toastError('并发上限更新失败', err);
      draw();
    }
  }

  function toggleTech() {
    S.showTech = !S.showTech;
    draw();
  }

  // ---------------------------------------------------------------- 菜单

  function onDocMouseDown(event) {
    if (event.target.closest && event.target.closest('.menu')) return;
    closeMenu();
  }

  function onMenuKey(event) {
    if (event.key === 'Escape') closeMenu();
  }

  function openMenu() {
    S.menuOpen = true;
    document.addEventListener('mousedown', onDocMouseDown, true);
    document.addEventListener('keydown', onMenuKey, true);
    draw();
  }

  function closeMenu() {
    if (!S.menuOpen) return;
    S.menuOpen = false;
    document.removeEventListener('mousedown', onDocMouseDown, true);
    document.removeEventListener('keydown', onMenuKey, true);
    draw();
  }

  function menuAction(fn) {
    return async () => {
      closeMenu();
      await fn();
    };
  }

  const view = accountView(S, {
    draw,
    refresh,
    setAll,
    onSlotChange,
    onProductChange,
    onPriorityChange,
    onConcurrencyChange,
    testAccount,
    refreshToken,
    exportOne,
    toggleAccount,
    removeAccount,
    refreshCredits,
    doCheckin,
    doDailyChatPending,
    doDailyChatWeb,
    exportAll,
    assignSlots,
    toggleTech,
    openMenu,
    closeMenu,
    menuAction,
  });

  function onAccountsChanged() {
    refresh();
  }

  window.addEventListener(acct.ACCOUNTS_CHANGED, onAccountsChanged);
  draw();
  refresh();

  return {
    refresh,
    unmount() {
      window.removeEventListener(acct.ACCOUNTS_CHANGED, onAccountsChanged);
      document.removeEventListener('mousedown', onDocMouseDown, true);
      document.removeEventListener('keydown', onMenuKey, true);
      S.menuOpen = false;
    },
  };
}
