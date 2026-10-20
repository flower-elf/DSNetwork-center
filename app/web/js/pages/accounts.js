// 账号页：账号池列表与积分、令牌有效期、出口、身份、优先级等列；筛选、排序、分页与可用性分档都在 accounts/logic.js 里，这里只负责渲染与调用接口。

import { html, live, nothing, render, repeat } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getState } from '../core/store.js';
import { barColor, fmtCompact, fmtCredit, fmtNumber, realmLabel } from '../core/format.js';
import { confirmDialog } from '../ui/dialog.js';
import { toast, toastError } from '../ui/toast.js';
import {
  badge, busyClick, chip, emptyState, loadErrorInline, loadErrorPage, pageHeader, pager,
  progressBar, realmBadge, skeletonRows, statCard,
} from '../ui/widgets.js';
import * as logic from './accounts/logic.js';
import * as activity from './accounts/activity.js';
import * as acct from './accounts/actions.js';
import { openAddAccountDialog, openImportDialog, openModelExcludeDialog, openNoteDialog } from './accounts/dialogs.js';

export const autoRefresh = true;

export function mount(host) {
  const S = {
    accounts: [],
    usage: {},
    slots: [],
    activity: {},
    standDown: null,
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
    // 账号列表与三份辅助数据互不依赖，一起发出；旧实现先等账号列表，首屏多一个往返。
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

  async function doCheckin() {
    try {
      const result = await acct.checkin();
      const results = result.results || [];
      applyAccountPayload(result);
      draw();
      if (!results.length) toast.warn('没有可领的国内版账号');
      else if (logic.anyOk(results)) toast.success(activity.BUDDY_LABEL + '签到', logic.summarizeResults(results, { okText: '已领取' }));
      else toast.warn(activity.BUDDY_LABEL + '签到', logic.summarizeResults(results));
    } catch (err) {
      toastError('签到失败', err);
    }
  }

  async function doDailyChatPending() {
    const pending = activity.pendingDailyChat(S.accounts, S.activity);
    if (!pending.length) {
      toast.success('国际版账号今天都已打卡');
      return;
    }
    try {
      const result = await acct.dailyChatPending(pending.map((account) => account.uid));
      const results = result.results || [];
      applyAccountPayload(result);
      await refresh();
      if (!results.length) toast.warn('名单里的账号没有执行');
      else if (logic.anyOk(results)) toast.success('未活跃打卡', logic.summarizeResults(results, { okText: '打卡成功' }));
      else toast.warn('未活跃打卡', logic.summarizeResults(results));
    } catch (err) {
      toastError('打卡失败', err);
    }
  }

  async function doDailyChatWeb() {
    const go = await confirmDialog({
      title: '确认网页通道打卡？',
      desc: '会为每个已启用的国际版账号各建一个网页端会话。这会实际发起一次任务并消耗该账号少量积分，用于计入官方「每日活跃」。',
      confirmText: '我已了解，继续',
    });
    if (!go) return;
    try {
      const result = await acct.dailyChatWeb();
      const results = result.results || [];
      applyAccountPayload(result);
      draw();
      if (!results.length) toast.warn('没有可打卡的国际版账号');
      else if (logic.anyOk(results)) {
        toast.success('网页通道打卡', logic.summarizeResults(results, { okText: '已建会话', withDetail: 'conversation' }));
      } else toast.warn('网页通道打卡', logic.summarizeResults(results));
    } catch (err) {
      toastError('网页通道打卡失败', err);
    }
  }

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

  function onPriorityChange(account, raw) {
    const parsed = logic.parsePriorityInput(raw);
    if (!parsed.ok) {
      toast.warn(parsed.message);
      draw();
      return;
    }
    if (parsed.value === logic.priorityOf(account)) {
      draw();
      return;
    }
    setPriority(account.uid, parsed.value);
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

  // ---------------------------------------------------------------- 片段

  function accountInfo(account) {
    const uid = String(account.uid || '');
    return html`
      <div class="account-cell">
        <div class="grow">
          <div class="row-wrap">
            <span class="strong">${account.nickname || uid.slice(0, 8)}</span>
            ${realmBadge(account.realm)}
            ${(account.disabledModels || []).length ? badge(
              '排除 ' + account.disabledModels.length + ' 个模型', 'muted',
              { small: true, title: logic.disabledModelsTitle(account) }) : nothing}
          </div>
          ${account.note ? html`<div class="cell-sub">备注：${account.note}</div>` : nothing}
          ${S.showTech
            ? html`
              <div class="cell-mono"><span class="cell-label">UID</span>${uid}</div>
              ${account.file ? html`<div class="cell-mono"><span class="cell-label">凭据文件名</span>${account.file}</div>` : nothing}`
            : nothing}
        </div>
      </div>`;
  }

  function standDownCard() {
    return html`
      <div class="card">
        <div class="card-head">
          <div>
            <h2 class="card-title tone-warning">${icon('TriangleAlert')}网关已停止</h2>
            <p class="card-sub">一个账号被上游限制（403），网关已停用全部账号，不再发送任何请求，避免把其他账号关联进来。确认上游已解封后点「全部启用」恢复。</p>
          </div>
          <button class="btn btn-primary btn-sm" @click=${busyClick(() => setAll(true))}>${icon('CircleCheck')}全部启用</button>
        </div>
      </div>`;
  }

  function activityCards(realm) {
    const summary = activity.todaySummary(S.accounts, S.activity, realm);
    const pendingHint = summary.pending.length
      ? '待处理：' + activity.namesOf(summary.pending).join('、')
      : '今天全部完成';
    if (realm === 'cn') {
      const streak = activity.streakSummary(S.accounts);
      return html`
        <div class="grid-stats">
          ${statCard({
            label: '本期 Buddy 加油站活动',
            value: activity.summaryText(summary, '已领', '未领'),
            icon: 'Fuel',
            tone: summary.total && summary.done === summary.total ? 'success' : 'warning',
            hint: pendingHint,
          })}
          ${statCard({
            label: '成长计划今日上报',
            value: streak.total ? streak.done + '/' + streak.total : '无账号',
            icon: 'Flame',
            tone: streak.total && streak.done === streak.total ? 'success' : 'warning',
            hint: '今天已完成活跃上报的账号数；连续天数在任务页查看',
          })}
        </div>`;
    }
    return html`
      <div class="grid-stats">
        ${statCard({
          label: '今日活跃打卡状态',
          value: activity.summaryText(summary, '已打卡', '未打卡'),
          icon: 'CalendarCheck',
          tone: summary.total && summary.done === summary.total ? 'success' : 'warning',
          hint: pendingHint,
        })}
      </div>`;
  }

  function activityBadges(account) {
    const status = activity.activityView(account, S.activity);
    const tone = (item) => (item.kind === 'ok' || item.kind === 'done' ? 'success'
      : item.kind === 'failed' ? 'danger' : 'muted');
    if (status.realm === 'cn') {
      const gas = status.gas;
      const streak = status.streak;
      return html`
        ${badge(activity.BUDDY_LABEL + ' · ' + gas.text, tone(gas), {
          small: true,
          title: gas.hint + (gas.at ? ' · ' + gas.at : ''),
        })}
        ${badge('成长计划 · ' + streak.text, tone(streak), {
          small: true,
          title: streak.hint + (streak.at ? ' · ' + streak.at : ''),
        })}`;
    }
    const daily = status.daily;
    return badge('今日活跃 · ' + daily.text, tone(daily), {
      small: true,
      title: daily.hint + (daily.at ? ' · ' + daily.at : ''),
    });
  }

  function statusInfo(account, now) {
    const view = logic.availabilityBadge(account, now);
    const pills = logic.cooldownPills(account, now);
    const error = logic.errorLine(account, now);
    return html`
      <div class="status-cell">
        ${badge(view.text, view.tone, { title: view.title })}
        ${pills.map((pill) => badge(pill.text, 'warning', { small: true, title: pill.title }))}
        ${activityBadges(account)}
      </div>
      ${error ? html`<div class="cell-sub tone-warning">${error}</div>` : nothing}`;
  }

  function creditsInfo(account, now) {
    const credits = account.credits;
    if (!credits) {
      return html`
        <div class="muted">未查询</div>
        <div class="cell-sub">点右上角「刷新积分」立即查询</div>`;
    }
    const age = logic.creditAgeText(credits, now);
    const expiry = logic.creditExpiryChip(account, now);
    const barPct = logic.creditBarPct(account, now);
    const valid = logic.validCredits(account, now);
    return html`
      <div class="credit-cell">
        ${barPct === null ? nothing : progressBar(barPct, barColor(barPct))}
        <div class="strong nums">${fmtCredit(valid.remain)}${valid.size > 0 ? html`<span class="muted"> / ${fmtCredit(valid.size)}</span>` : nothing}</div>
        ${age ? html`<div class="cell-sub">${age}</div>` : nothing}
        ${expiry ? badge(expiry.text, expiry.tone, { small: true, title: expiry.title }) : nothing}
      </div>`;
  }

  function tokenInfo(account, now) {
    const view = logic.tokenExpiryView(account, now);
    return html`
      <div class="token-cell">
        ${view.pct === null ? nothing : progressBar(view.pct, barColor(view.pct))}
        <span class="cell-sub tone-${view.tone}">${view.text}</span>
      </div>`;
  }

  function usageInfo(account) {
    const row = logic.usageFor(S.usage, account.uid);
    return html`
      <div class="nums">${fmtNumber(row.requests)} 次</div>
      <div class="cell-sub nums">${fmtCompact(row.total_tokens)} token</div>`;
  }

  function slotSelect(account) {
    const view = logic.slotOptions(account, S.slots);
    return html`
      <select class="select input-sm" .value=${view.selected} title="这个账号的出站线路，直连表示不经过代理槽，旧代理是账号上遗留的单账号代理"
              @change=${(event) => onSlotChange(account, event.target.value)}>
        ${view.options.map((option) => html`<option value=${option.value} .selected=${option.value === view.selected}>${option.label}</option>`)}
      </select>`;
  }

  function identitySelect(account) {
    const view = logic.productOptions(account);
    // 选中项写在 option 上：lit 先设置 select.value 再插入 option，首次渲染时
    // select 里还没有选项，只靠 .value 会退回第一项，刷新后显示成 WB。
    return html`
      <select class="select input-sm" .value=${view.selected} title=${logic.IDENTITY_TIP}
              @change=${(event) => onProductChange(account, event.target.value)}>
        ${view.options.map((option) => html`<option value=${option.value} .selected=${option.value === view.selected}>${option.label}</option>`)}
      </select>`;
  }

  function priorityInput(account) {
    const draft = logic.draftFor(S.draft, logic.uidAttr(account.uid), 'priority');
    const value = draft === null ? String(logic.priorityOf(account)) : draft;
    return html`
      <input class="input input-num input-sm input-priority" type="number" min="0" max="9999" step="1"
             data-draft="priority" data-uid=${logic.uidAttr(account.uid)}
             title="调用优先级：数字越小越先被选中，数字相同的按轮询顺序。默认 100，允许 0 到 9999。"
             .value=${live(value)}
             @change=${(event) => onPriorityChange(account, event.target.value)}>`;
  }

  function concurrencyInput(account) {
    const draft = logic.draftFor(S.draft, logic.uidAttr(account.uid), 'concurrency');
    const value = draft === null ? String(logic.concurrencyOf(account)) : draft;
    const busy = logic.activeRequestsOf(account);
    return html`
      <input class="input input-num input-sm input-concurrency" type="number" min="0" max="1000" step="1"
             data-draft="concurrency" data-uid=${logic.uidAttr(account.uid)}
             title=${'单账号单模型的并发上限：同一账号每个模型各自计数，0 表示不限。当前正在服务 ' + busy + ' 个请求。'}
             .value=${live(value)}
             @change=${(event) => onConcurrencyChange(account, event.target.value)}>`;
  }

  function onConcurrencyChange(account, raw) {
    const parsed = logic.parseConcurrencyInput(raw);
    if (!parsed.ok) {
      toast.warn(parsed.message);
      draw();
      return;
    }
    if (parsed.value === logic.concurrencyOf(account)) {
      draw();
      return;
    }
    setConcurrencyLimit(account.uid, parsed.value);
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

  function rowActions(account) {
    const name = account.nickname || String(account.uid || '').slice(0, 8);
    return html`
      <div class="row-actions">
        <button class="btn btn-ghost btn-icon btn-xs" title="测试：用这个账号向它自己的端点发送一条 hi 请求"
                @click=${busyClick(() => testAccount(account))}>${icon('Zap')}</button>
        <button class="btn btn-ghost btn-icon btn-xs" title="刷新 Token：向腾讯换取新的访问令牌并保存"
                @click=${busyClick(() => refreshToken(account))}>${icon('KeyRound')}</button>
        <button class="btn btn-ghost btn-icon btn-xs" title="模型排除：这个账号不接的模型"
                @click=${() => openModelExcludeDialog(account, { onDone: () => refresh() })}>${icon('CircleSlash')}</button>
        <button class="btn btn-ghost btn-icon btn-xs" title="备注：只保存在本机，最长 100 字"
                @click=${() => openNoteDialog(account)}>${icon('StickyNote')}</button>
        <button class="btn btn-ghost btn-icon btn-xs" title="导出这个账号"
                @click=${busyClick(() => exportOne(account))}>${icon('Download')}</button>
        <button class="btn btn-ghost btn-icon btn-xs" title=${account.enabled ? '停用这个账号' : '启用这个账号'}
                @click=${busyClick(() => toggleAccount(account, !account.enabled))}>${icon(account.enabled ? 'Ban' : 'CircleCheck')}</button>
        <button class="btn btn-ghost btn-icon btn-xs tone-danger" title=${'删除账号「' + name + '」'} aria-label="删除账号"
                @click=${() => removeAccount(account)}>${icon('Trash2')}</button>
      </div>`;
  }

  function tableRow(account, now) {
    return html`
      <tr>
        <td>${accountInfo(account)}</td>
        <td>${statusInfo(account, now)}</td>
        <td>${creditsInfo(account, now)}</td>
        <td>${tokenInfo(account, now)}</td>
        <td>${slotSelect(account)}</td>
        <td>${identitySelect(account)}</td>
        <td>${priorityInput(account)}</td>
        <td>${concurrencyInput(account)}</td>
        <td class="right">${usageInfo(account)}</td>
        <td class="actions">${rowActions(account)}</td>
      </tr>`;
  }

  function mobileCard(account, now) {
    const view = logic.availabilityBadge(account, now);
    const pills = logic.cooldownPills(account, now);
    const error = logic.errorLine(account, now);
    return html`
      <div class="mobile-item">
        <div class="row-between">
          ${accountInfo(account)}
          ${badge(view.text, view.tone, { title: view.title })}
        </div>
        ${pills.length ? html`<div class="row-wrap">${pills.map((pill) => badge(pill.text, 'warning', { small: true, title: pill.title }))}</div>` : nothing}
        ${error ? html`<div class="text-2xs tone-warning">${error}</div>` : nothing}
        <div class="mobile-grid">
          <div class="mobile-field">
            <span class="field-label">积分</span>
            ${creditsInfo(account, now)}
          </div>
          <div class="mobile-field">
            <span class="field-label">访问令牌有效期</span>
            ${tokenInfo(account, now)}
          </div>
          <div class="mobile-field">
            <span class="field-label">出口</span>
            ${slotSelect(account)}
          </div>
          <div class="mobile-field">
            <span class="field-label">身份</span>
            ${identitySelect(account)}
          </div>
          <div class="mobile-field">
            <span class="field-label">优先级</span>
            ${priorityInput(account)}
          </div>
          <div class="mobile-field">
            <span class="field-label">并发上限</span>
            ${concurrencyInput(account)}
          </div>
          <div class="mobile-field">
            <span class="field-label">用量</span>
            ${usageInfo(account)}
          </div>
        </div>
        ${rowActions(account)}
      </div>`;
  }

  function filterCard(counts) {
    return html`
      <div class="card filter-card">
        <div class="input-search">
          ${icon('Search')}
          <input class="input" placeholder="搜索昵称 / UID / 文件名 / 备注" data-draft="query" data-uid=""
                 .value=${live(logic.draftFor(S.draft, '', 'query') ?? S.query)}
                 @input=${(event) => { S.query = event.target.value; S.page = 1; draw(); }}>
        </div>
        <div class="chips">
          <span class="chips-label">${icon('ListFilter')}状态</span>
          ${logic.FILTERS.map((item) => chip(item.label, S.filter === item.id, () => {
            S.filter = item.id;
            S.page = 1;
            draw();
          }, counts[item.id]))}
        </div>
        <div class="chips">
          <span class="chips-label">${icon('ArrowUpDown')}排序</span>
          <select class="select input-sm" .value=${S.sort} @change=${(event) => {
            S.sort = event.target.value;
            S.page = 1;
            draw();
          }}>
            ${logic.SORTS.map((item) => html`<option value=${item.id}>${item.label}</option>`)}
          </select>
        </div>
      </div>`;
  }

  function emptyCard(kind) {
    const realm = getState().viewRealm;
    if (kind === 'none') {
      return emptyState({
        icon: 'Users',
        title: '账号池里还没有账号',
        desc: '登录一个腾讯账号之后，网关才能把请求转发出去，账号的积分与令牌状态也会显示在这里。',
        action: html`<button class="btn btn-primary btn-sm" @click=${() => openAddAccountDialog()}>${icon('UserPlus')}添加账号 (OAuth)</button>`,
      });
    }
    if (kind === 'realm') {
      return emptyState({
        icon: 'Users',
        title: realmLabel(realm) + '还没有账号',
        desc: '账号池里没有属于这个版本的账号，其它版本的账号不会显示在这里。可以切换右上角「查看的版本」，或者登录一个该版本的账号。',
        action: html`<button class="btn btn-primary btn-sm" @click=${() => openAddAccountDialog({ realm })}>${icon('UserPlus')}添加账号 (OAuth)</button>`,
      });
    }
    return emptyState({
      icon: 'SearchX',
      title: '没有匹配的账号',
      desc: '换个关键词，或者把状态筛选切回「全部」，已停用的账号需要在「已停用」里查看。',
      action: html`<button class="btn btn-outline btn-sm" @click=${() => {
        S.query = '';
        S.filter = 'all';
        S.page = 1;
        draw();
      }}>${icon('RotateCcw')}清除筛选</button>`,
    });
  }

  function listCard(page, kind, now) {
    if (kind) {
      return html`<div class="card card-flush">${emptyCard(kind)}</div>`;
    }
    return html`
      <div class="card card-flush accounts-list">
        <div class="table-wrap desktop-only">
          <table class="table table-accounts">
            <thead>
              <tr>
                <th>账号</th>
                <th>状态</th>
                <th>积分</th>
                <th>访问令牌有效期</th>
                <th>出口</th>
                <th>身份</th>
                <th>优先级</th>
                <th>并发上限</th>
                <th class="right">用量</th>
                <th class="actions">操作</th>
              </tr>
            </thead>
            <tbody>${repeat(page.items, (account) => account.uid, (account) => tableRow(account, now))}</tbody>
          </table>
        </div>
        <div class="mobile-list">${page.items.map((account) => mobileCard(account, now))}</div>
        ${pager({
          page: page.page,
          pages: page.pages,
          total: page.total,
          unit: '个账号',
          onPage: (next) => { S.page = next; draw(); },
        })}
        <div class="card-note">
          ${icon('Info')}
          <span class="grow">${logic.IDENTITY_TIP}多个账号按会话粘性与轮询使用，某个账号被上游拒绝时自动切到下一个。</span>
        </div>
      </div>`;
  }

  function headerActions(realm) {
    const acts = logic.realmActions(realm);
    return html`
      <button class="btn btn-primary btn-sm" @click=${() => openAddAccountDialog({ realm })}>${icon('UserPlus')}添加账号 (OAuth)</button>
      <button class="btn btn-outline btn-sm" title="直接向腾讯查询各账号当前积分，上游缓存的积分可能滞后数小时"
              @click=${busyClick(refreshCredits)}>${icon('RefreshCw')}刷新积分</button>
      ${acts.checkin ? html`
        <button class="btn btn-outline btn-sm" title="为每个国内版账号领取当天的 Buddy 加油站积分"
                @click=${busyClick(doCheckin)}>${icon('Fuel')}${activity.BUDDY_LABEL}签到</button>` : nothing}
      ${acts.dailyChat ? html`
        <button class="btn btn-outline btn-sm" title="今天还没打卡的已启用国际版账号；已经打过的不再打第二遍"
                @click=${busyClick(doDailyChatPending)}>${icon('CalendarCheck')}未活跃打卡一键打卡</button>` : nothing}
      ${acts.dailyChatWeb ? html`
        <button class="btn btn-outline btn-sm" title="只建网页端会话，官方每日活跃只认网页端对话；会消耗少量积分"
                @click=${doDailyChatWeb}>${icon('Globe')}网页通道打卡</button>` : nothing}
      <div class="menu">
        <button class="btn btn-outline btn-sm menu-toggle" aria-haspopup="menu" aria-expanded=${S.menuOpen ? 'true' : 'false'}
                @click=${() => (S.menuOpen ? closeMenu() : openMenu())}>${icon('Ellipsis')}更多</button>
        ${S.menuOpen ? html`
          <div class="menu-panel" role="menu">
            <button class="menu-item" role="menuitem" @click=${busyClick(menuAction(exportAll))}>${icon('Download')}导出账号</button>
            <button class="menu-item" role="menuitem" @click=${menuAction(() => openImportDialog({ onDone: () => refresh() }))}>${icon('Upload')}导入账号</button>
            <button class="menu-item" role="menuitem" title="为尚未绑定出口的已启用账号依次分配已启用的代理槽，已有绑定的账号不受影响"
                    @click=${busyClick(menuAction(assignSlots))}>${icon('Network')}分配代理出口给未绑定账号</button>
            <button class="menu-item" role="menuitem" title="在账号列里显示或隐藏每个账号的 UID 与凭据文件名"
                    @click=${menuAction(() => { S.showTech = !S.showTech; draw(); })}>${icon(S.showTech ? 'EyeOff' : 'Eye')}${S.showTech ? '隐藏技术信息' : '显示技术信息'}</button>
            <div class="menu-sep"></div>
            <button class="menu-item" role="menuitem" @click=${busyClick(menuAction(() => setAll(true)))}>${icon('CircleCheck')}全部启用</button>
            <button class="menu-item tone-danger" role="menuitem" @click=${busyClick(menuAction(() => setAll(false)))}>${icon('Ban')}全部停用</button>
          </div>` : nothing}
      </div>`;
  }

  function view() {
    const realm = getState().viewRealm;
    const now = Date.now();
    const visible = logic.accountsForRealm(S.accounts, realm);
    const counts = logic.groupCounts(visible, now);
    const selected = logic.selectAccounts(visible, { query: S.query, filter: S.filter, sort: S.sort }, now);
    const page = logic.paginate(selected, S.page);
    S.page = page.page;
    const kind = logic.emptyKind(S.accounts, visible, selected);
    const firstScreen = S.loading && !S.accounts.length;
    return html`
      <div class="page">
        ${pageHeader({
          path: 'accounts',
          title: '账号管理',
          desc: realm === 'cn'
            ? '国内版账号池：访问令牌有效期、积分、Buddy 加油站与连通性'
            : '国际版账号池：访问令牌有效期、积分、出口线路与连通性',
          actions: headerActions(realm),
        })}
        ${S.error ? loadErrorPage(S.error, refresh) : nothing}
        ${!S.error && S.partial ? loadErrorInline(S.partial, refresh) : nothing}
        ${S.standDown && S.standDown.active ? standDownCard() : nothing}
        ${!S.error && visible.length ? activityCards(realm) : nothing}
        ${!S.error && visible.length ? filterCard(counts) : nothing}
        ${firstScreen ? skeletonRows(6) : nothing}
        ${!firstScreen && !S.error ? listCard(page, kind, now) : nothing}
      </div>`;
  }

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
