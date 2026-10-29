// 账号页的模板片段。S 是页面状态，h 是 accounts.js 提供的操作入口；这里只拼模板，不发请求。
import { html, live, nothing, repeat } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { getState } from '../../core/store.js';
import { barColor, fmtCompact, fmtCredit, fmtNumber, realmLabel } from '../../core/format.js';
import {
  badge, busyClick, chip, emptyState, loadErrorInline, loadErrorPage, pageHeader, pager,
  progressBar, realmBadge, skeletonRows, statCard,
} from '../../ui/widgets.js';
import * as logic from './logic.js';
import * as activity from './activity.js';
import { openAddAccountDialog } from './dialogs/add.js';
import { openImportDialog } from './dialogs/import.js';
import { openNoteDialog } from './dialogs/note.js';
import { openModelExcludeDialog } from './dialogs/exclude.js';

export function accountView(S, h) {
  function accountInfo(account) {
    const uid = String(account.uid || '');
    return html`
      <div class="account-cell">
        <div class="grow">
          <div class="row-wrap">
            <span class="strong">${account.nickname || uid.slice(0, 8)}</span>
            ${realmBadge(account.realm)}
            ${account.apiKey ? badge('API Key', 'muted', {
              small: true, title: account.credentialMask || '静态 API Key' }) : nothing}
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
          <button class="btn btn-primary btn-sm" @click=${busyClick(() => h.setAll(true))}>${icon('CircleCheck')}全部启用</button>
        </div>
      </div>`;
  }

  function activityCards(realm) {
    const summary = activity.todaySummary(S.accounts, S.activity, realm);
    if (realm === 'cn') {
      const streak = activity.streakSummary(S.accounts);
      return html`
        <div class="grid-stats">
          ${statCard({
            label: '本期 Buddy 加油站活动',
            icon: 'Fuel',
            ...activity.summaryStat(summary, { doneWord: '已领', pendingWord: '未领' }),
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
          icon: 'CalendarCheck',
          ...activity.summaryStat(summary, { doneWord: '已打卡', pendingWord: '未打卡' }),
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
              @change=${(event) => h.onSlotChange(account, event.target.value)}>
        ${view.options.map((option) => html`<option value=${option.value} .selected=${option.value === view.selected}>${option.label}</option>`)}
      </select>`;
  }

  function identitySelect(account) {
    const view = logic.productOptions(account);
    // 选中项写在 option 上：lit 先设置 select.value 再插入 option，首次渲染时
    // select 里还没有选项，只靠 .value 会退回第一项，刷新后显示成 WB。
    return html`
      <select class="select input-sm" .value=${view.selected} title=${logic.IDENTITY_TIP}
              @change=${(event) => h.onProductChange(account, event.target.value)}>
        ${view.options.map((option) => html`<option value=${option.value} .selected=${option.value === view.selected}>${option.label}</option>`)}
      </select>`;
  }

  function numberCell(account, { field, draftField, value, min, max, title, onChange }) {
    return html`
      <input class="input input-num input-sm input-${field}" type="number" min=${min} max=${max} step="1"
             data-draft=${draftField} data-uid=${logic.uidAttr(account.uid)}
             title=${title}
             .value=${live(value)}
             @change=${(event) => onChange(event.target.value)}>`;
  }

  function priorityInput(account) {
    const limits = logic.accountLimits(S.limits);
    const draft = logic.draftFor(S.draft, logic.uidAttr(account.uid), 'priority');
    const value = draft === null ? String(logic.priorityOf(account)) : draft;
    return numberCell(account, {
      field: 'priority',
      draftField: 'priority',
      value,
      min: 0,
      max: limits.priorityMax,
      title: '调用优先级：网关先选数字较小的账号，数字相同的账号按轮询顺序选择。默认 '
        + limits.priorityDefault + '，允许 0 到 ' + limits.priorityMax + '。',
      onChange: (raw) => h.onPriorityChange(account, raw),
    });
  }

  function concurrencyInput(account) {
    const limits = logic.accountLimits(S.limits);
    const draft = logic.draftFor(S.draft, logic.uidAttr(account.uid), 'concurrency');
    const value = draft === null ? String(logic.concurrencyOf(account)) : draft;
    const busy = logic.activeRequestsOf(account);
    return numberCell(account, {
      field: 'concurrency',
      draftField: 'concurrency',
      value,
      min: 0,
      max: limits.concurrencyMax,
      title: '单账号单模型的并发上限：同一账号每个模型各自计数，0 表示不限。当前正在服务 '
        + busy + ' 个请求。',
      onChange: (raw) => h.onConcurrencyChange(account, raw),
    });
  }

  function rowActions(account) {
    const name = account.nickname || String(account.uid || '').slice(0, 8);
    return html`
      <div class="row-actions">
        <button class="btn btn-ghost btn-icon btn-xs" title="测试：用这个账号向它自己的端点发送一条 hi 请求"
                @click=${busyClick(() => h.testAccount(account))}>${icon('Zap')}</button>
        ${account.apiKey ? nothing : html`
          <button class="btn btn-ghost btn-icon btn-xs" title="刷新 Token：向腾讯换取新的访问令牌并保存"
                  @click=${busyClick(() => h.refreshToken(account))}>${icon('KeyRound')}</button>`}
        <button class="btn btn-ghost btn-icon btn-xs" title="模型排除：这个账号不接的模型"
                @click=${() => openModelExcludeDialog(account, { onDone: () => h.refresh() })}>${icon('CircleSlash')}</button>
        <button class="btn btn-ghost btn-icon btn-xs"
                title=${'备注：只保存在本机，最长 ' + logic.accountLimits(S.limits).noteMax + ' 字'}
                @click=${() => openNoteDialog(account, { limits: S.limits })}>${icon('StickyNote')}</button>
        <button class="btn btn-ghost btn-icon btn-xs" title="导出这个账号"
                @click=${busyClick(() => h.exportOne(account))}>${icon('Download')}</button>
        <button class="btn btn-ghost btn-icon btn-xs" title=${account.enabled ? '停用这个账号' : '启用这个账号'}
                @click=${busyClick(() => h.toggleAccount(account, !account.enabled))}>${icon(account.enabled ? 'Ban' : 'CircleCheck')}</button>
        <button class="btn btn-ghost btn-icon btn-xs tone-danger" title=${'删除账号「' + name + '」'} aria-label="删除账号"
                @click=${() => h.removeAccount(account)}>${icon('Trash2')}</button>
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
                 @input=${(event) => { S.query = event.target.value; S.page = 1; h.draw(); }}>
        </div>
        <div class="chips">
          <span class="chips-label">${icon('ListFilter')}状态</span>
          ${logic.FILTERS.map((item) => chip(item.label, S.filter === item.id, () => {
            S.filter = item.id;
            S.page = 1;
            h.draw();
          }, counts[item.id]))}
        </div>
        <div class="chips">
          <span class="chips-label">${icon('ArrowUpDown')}排序</span>
          <select class="select input-sm" .value=${S.sort} @change=${(event) => {
            S.sort = event.target.value;
            S.page = 1;
            h.draw();
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
        action: html`<button class="btn btn-primary btn-sm" @click=${() => openAddAccountDialog()}>${icon('UserPlus')}添加账号</button>`,
      });
    }
    if (kind === 'realm') {
      return emptyState({
        icon: 'Users',
        title: realmLabel(realm) + '还没有账号',
        desc: '账号池里没有属于这个版本的账号，其它版本的账号不会显示在这里。可以切换右上角「查看的版本」，或者登录一个该版本的账号。',
        action: html`<button class="btn btn-primary btn-sm" @click=${() => openAddAccountDialog({ realm })}>${icon('UserPlus')}添加账号</button>`,
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
        h.draw();
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
          onPage: (next) => { S.page = next; h.draw(); },
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
      <button class="btn btn-primary btn-sm" @click=${() => openAddAccountDialog({ realm })}>${icon('UserPlus')}添加账号</button>
      <button class="btn btn-outline btn-sm" title="直接向腾讯查询各账号当前积分，上游缓存的积分可能滞后数小时"
              @click=${busyClick(h.refreshCredits)}>${icon('RefreshCw')}刷新积分</button>
      ${acts.checkin ? html`
        <button class="btn btn-outline btn-sm" title="为每个国内版账号领取当天的 Buddy 加油站积分"
                @click=${busyClick(h.doCheckin)}>${icon('Fuel')}${activity.BUDDY_LABEL}签到</button>` : nothing}
      ${acts.dailyChat ? html`
        <button class="btn btn-outline btn-sm" title="今天还没打卡的已启用国际版账号；已经打过的不再打第二遍"
                @click=${busyClick(h.doDailyChatPending)}>${icon('CalendarCheck')}未活跃账号一键打卡</button>` : nothing}
      ${acts.dailyChatWeb ? html`
        <button class="btn btn-outline btn-sm" title="只建网页端会话，官方每日活跃只认网页端对话；会消耗少量积分"
                @click=${h.doDailyChatWeb}>${icon('Globe')}网页通道打卡</button>` : nothing}
      <div class="menu">
        <button class="btn btn-outline btn-sm menu-toggle" aria-haspopup="menu" aria-expanded=${S.menuOpen ? 'true' : 'false'}
                @click=${() => (S.menuOpen ? h.closeMenu() : h.openMenu())}>${icon('Ellipsis')}更多</button>
        ${S.menuOpen ? html`
          <div class="menu-panel" role="menu">
            <button class="menu-item" role="menuitem" @click=${busyClick(h.menuAction(h.exportAll))}>${icon('Download')}导出账号</button>
            <button class="menu-item" role="menuitem" @click=${h.menuAction(() => openImportDialog({ onDone: () => h.refresh() }))}>${icon('Upload')}导入账号</button>
            <button class="menu-item" role="menuitem" title="为尚未绑定出口的已启用账号依次分配已启用的代理槽，已有绑定的账号不受影响"
                    @click=${busyClick(h.menuAction(h.assignSlots))}>${icon('Network')}分配代理出口给未绑定账号</button>
            <button class="menu-item" role="menuitem" title="在账号列里显示或隐藏每个账号的 UID 与凭据文件名"
                    @click=${h.menuAction(h.toggleTech)}>${icon(S.showTech ? 'EyeOff' : 'Eye')}${S.showTech ? '隐藏技术信息' : '显示技术信息'}</button>
            <div class="menu-sep"></div>
            <button class="menu-item" role="menuitem" @click=${busyClick(h.menuAction(() => h.setAll(true)))}>${icon('CircleCheck')}全部启用</button>
            <button class="menu-item tone-danger" role="menuitem" @click=${busyClick(h.menuAction(() => h.setAll(false)))}>${icon('Ban')}全部停用</button>
          </div>` : nothing}
      </div>`;
  }

  return function view() {
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
        ${S.error ? loadErrorPage(S.error, h.refresh) : nothing}
        ${!S.error && S.partial ? loadErrorInline(S.partial, h.refresh) : nothing}
        ${S.standDown && S.standDown.active ? standDownCard() : nothing}
        ${!S.error && visible.length ? activityCards(realm) : nothing}
        ${!S.error && visible.length ? filterCard(counts) : nothing}
        ${firstScreen ? skeletonRows(6) : nothing}
        ${!firstScreen && !S.error ? listCard(page, kind, now) : nothing}
      </div>`;
  };
}
