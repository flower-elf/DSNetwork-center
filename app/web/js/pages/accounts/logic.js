// 账号页的纯逻辑：可用性分档、筛选、排序、分页、积分与有效期口径；只依赖 core/format.js，可以在 Node 里直接测试。

import { fmtAgo, fmtNumber, toMillis } from '../../core/format.js';

export const PAGE_SIZE = 20;
export const PRIORITY_DEFAULT = 100;
export const PRIORITY_MAX = 9999;
//: 单账号单模型的并发上限，与服务端 MAX_ACCOUNT_CONCURRENCY 一致。
export const CONCURRENCY_MAX = 1000;

/** 四个展示分组，顺序即筛选芯片顺序：可用、需处理、冷却中、已停用。 */
export const AVAILABILITY_GROUPS = ['usable', 'attention', 'cooling', 'stopped'];

export const FILTERS = [
  { id: 'all', label: '全部' },
  { id: 'usable', label: '可用' },
  { id: 'attention', label: '需处理' },
  { id: 'cooling', label: '冷却中' },
  { id: 'stopped', label: '已停用' },
];

export const SORTS = [
  { id: 'default', label: '默认顺序' },
  { id: 'expiry', label: '有效期近的在前' },
  { id: 'credit', label: '积分少的在前' },
  { id: 'priority', label: '优先级' },
];

export const PRODUCTS = [
  { id: 'workbuddy', short: 'WB', label: 'WorkBuddy 独立桌面客户端' },
  { id: 'vscode', short: 'VSC', label: '官方 VSCode 插件' },
  { id: 'cli', short: 'CLI', label: '官方 CodeBuddy CLI' },
];

export const IDENTITY_TIP = '出站身份：WB 是 WorkBuddy 独立桌面客户端，VSC 是官方 VSCode 插件，CLI 是官方 CodeBuddy CLI。三者对应不同的出站指纹与配额通道。';

/** 秒级时间戳归一化：非法或非正数按 0 处理。 */
function seconds(value) {
  const n = Number(value);
  return Number.isFinite(n) && n > 0 ? n : 0;
}

/** 令牌是否已经过期。expiresAt 缺失时改用服务端给出的文本判断。 */
export function tokenExpired(account, now = Date.now()) {
  const ms = toMillis(account && account.expiresAt);
  if (ms !== null) return ms <= now;
  return String((account && account.expiresIn) || '') === 'expired';
}

/** 仍然生效的模型级冷却，按恢复时刻升序。 */
export function activeModelCooldowns(account, now = Date.now()) {
  const list = Array.isArray(account && account.modelCooldowns) ? account.modelCooldowns : [];
  return list
    .map((item) => ({ model: String((item && item.model) || '?'), at: seconds(item && item.expiresAt) * 1000 }))
    .filter((item) => item.at > now)
    .sort((x, y) => x.at - y.at || (x.model < y.model ? -1 : 1));
}

/** 冷却徽章文案：模型名加本地恢复时刻。 */
export function cooldownPills(account, now = Date.now()) {
  return activeModelCooldowns(account, now).map((item) => ({
    model: item.model,
    text: item.model + ' · ' + fmtClock(item.at) + ' 恢复',
    title: '只有这个模型暂时受限，账号仍可服务其它模型。恢复时刻按本机时区显示。',
  }));
}

const pad2 = (n) => String(n).padStart(2, '0');

/** 本地时间「月-日 时:分」。 */
export function fmtClock(ms) {
  const d = new Date(Number(ms));
  if (!Number.isFinite(d.getTime())) return '—';
  return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

/** 行内错误提示。模型限流另有专门的徽章，不再重复显示这条原文。 */
export function errorLine(account, now = Date.now()) {
  const text = String((account && account.lastError) || '').trim();
  if (!text) return '';
  if (text === 'HTTP 429 (model throttled)' && activeModelCooldowns(account, now).length) return '';
  return text;
}

/** 账号级可用性档位，顺序即判定优先级。 */
export function availabilityTier(account, now = Date.now()) {
  if (!account) return 'disabled';
  if (!account.enabled) return 'disabled';
  if (tokenExpired(account, now)) return 'expired';
  if (account.inCooldown) return 'cooling';
  if (account.reserveBlocked) return 'reserve';
  if (account.dailyLimitBlocked) return 'dailyLimit';
  if (errorLine(account, now)) return 'error';
  if (activeModelCooldowns(account, now).length) return 'modelCooled';
  return 'usable';
}

const TIER_GROUP = {
  disabled: 'stopped',
  expired: 'attention',
  reserve: 'attention',
  dailyLimit: 'attention',
  error: 'attention',
  cooling: 'cooling',
  modelCooled: 'cooling',
  usable: 'usable',
};

export function availabilityGroup(tier) {
  return TIER_GROUP[tier] || 'attention';
}

const TIER_NOTES = {
  disabled: '停用期间不参与任何调度，启用后自动恢复。',
  expired: '令牌已经过期，先点「刷新 Token」；若刷新失败，需要重新添加这个账号。',
  cooling: '账号被上游拒绝后进入冷却，冷却结束前不会被选中。',
  reserve: '剩余积分低于保留阈值，暂停接单以免把积分用完。',
  error: '这个账号最近一次调用失败，原因写在表格里。',
  modelCooled: '账号本身仍可用，只是个别模型暂时受限，恢复后自动重新参与调度。',
  usable: '',
};

const TIER_BADGE = {
  disabled: { text: '已停用', tone: 'muted' },
  expired: { text: '已过期', tone: 'danger' },
  cooling: { text: '冷却中', tone: 'warning' },
  reserve: { text: '保留积分', tone: 'warning' },
  dailyLimit: { text: '日限额', tone: 'warning' },
  error: { text: '最近出错', tone: 'severe' },
  modelCooled: { text: '模型冷却', tone: 'warning' },
  usable: { text: '可用', tone: 'success' },
};

/** 状态徽章的文案、色调与说明。日限额把当天用量写进说明里。 */
export function availabilityBadge(account, now = Date.now()) {
  const tier = availabilityTier(account, now);
  const spec = TIER_BADGE[tier];
  let title = TIER_NOTES[tier];
  if (tier === 'dailyLimit') {
    title = '今日已用 ' + fmtNumber((account && account.dailyTokensToday) || 0)
      + ' / ' + fmtNumber((account && account.dailyTokenLimit) || 0)
      + ' token，暂停接单，本地时间 0 点恢复。';
  }
  return { tier, group: availabilityGroup(tier), text: spec.text, tone: spec.tone, title };
}

/** 搜索命中范围：昵称、UID、账号文件名、备注。 */
export function matchQuery(account, query) {
  const q = String(query || '').trim().toLowerCase();
  if (!q) return true;
  return [account.nickname, account.uid, account.file, account.note]
    .some((value) => String(value || '').toLowerCase().includes(q));
}

/** 各分组的账号数，用于筛选芯片上的计数。 */
export function groupCounts(list, now = Date.now()) {
  const counts = { all: 0, usable: 0, attention: 0, cooling: 0, stopped: 0 };
  for (const account of list || []) {
    counts.all += 1;
    counts[availabilityGroup(availabilityTier(account, now))] += 1;
  }
  return counts;
}

/** 有效期排序键：没有到期时间的排最后，已经过期的排最前。 */
function expiryKey(account) {
  const ms = toMillis(account && account.expiresAt);
  return ms === null ? Number.POSITIVE_INFINITY : ms;
}

/** 积分排序键：还没查到积分的排最后。 */
function creditKey(account) {
  const remain = account && account.credits && Number(account.credits.remain);
  return Number.isFinite(remain) ? remain : Number.POSITIVE_INFINITY;
}

export function priorityOf(account) {
  const value = Number(account && account.priority);
  return Number.isInteger(value) ? value : PRIORITY_DEFAULT;
}

/** 并发上限；没设过或旧账号当作 0（不限）。 */
export function concurrencyOf(account) {
  const value = Number(account && account.concurrencyLimit);
  return Number.isInteger(value) && value > 0 ? value : 0;
}

/** 正在服务的请求数。 */
export function activeRequestsOf(account) {
  const value = Number(account && account.activeRequests);
  return Number.isInteger(value) && value > 0 ? value : 0;
}

/** 服务端下发的数值上限；缺少时使用本地默认值。 */
export function accountLimits(source) {
  const raw = source && typeof source === 'object' ? source : {};
  const number = (key, fallback) => {
    const value = Number(raw[key]);
    return Number.isFinite(value) && value >= 0 ? value : fallback;
  };
  return {
    priorityDefault: number('priorityDefault', PRIORITY_DEFAULT),
    priorityMax: number('priorityMax', PRIORITY_MAX),
    concurrencyMax: number('concurrencyMax', CONCURRENCY_MAX),
    weightMin: number('weightMin', 1),
    weightMax: number('weightMax', 100),
    noteMax: number('noteMax', 100),
    apiKeyPrefixes: Array.isArray(raw.apiKeyPrefixes) && raw.apiKeyPrefixes.length
      ? raw.apiKeyPrefixes : ['ck_', 'pt_'],
  };
}

/** 并发上限校验：0 或空表示不限，其余必须是正整数；与服务端 normalise_concurrency_limit 同一口径。 */
export function parseConcurrencyInput(raw, limits) {
  const max = accountLimits(limits).concurrencyMax;
  const text = String(raw == null ? '' : raw).trim();
  if (text === '') return { ok: true, value: 0 };
  const value = Number(text);
  if (!Number.isInteger(value) || value < 0 || value > max) {
    return { ok: false, message: '并发上限需要 0 到 ' + max + ' 之间的整数，填写 0 表示不限，本次没有发送请求' };
  }
  return { ok: true, value };
}

/** 排序。默认顺序把停用账号沉到最后，其余排序都保持稳定。 */
export function sortAccounts(list, sort = 'default') {
  const rows = (list || []).map((account, index) => ({ account, index }));
  if (sort === 'expiry') {
    rows.sort((x, y) => expiryKey(x.account) - expiryKey(y.account) || x.index - y.index);
  } else if (sort === 'credit') {
    rows.sort((x, y) => creditKey(x.account) - creditKey(y.account) || x.index - y.index);
  } else if (sort === 'priority') {
    rows.sort((x, y) => priorityOf(x.account) - priorityOf(y.account) || x.index - y.index);
  } else {
    rows.sort((x, y) => (x.account.enabled === y.account.enabled
      ? x.index - y.index
      : (x.account.enabled ? -1 : 1)));
  }
  return rows.map((row) => row.account);
}

/** 搜索、分组筛选与排序的组合入口。 */
export function selectAccounts(list, { query = '', filter = 'all', sort = 'default' } = {}, now = Date.now()) {
  const kept = (list || []).filter((account) => matchQuery(account, query)
    && (filter === 'all' || availabilityGroup(availabilityTier(account, now)) === filter));
  return sortAccounts(kept, sort);
}

/** 按当前查看的版本挑出账号。缺少版本字段的旧账号归入正在查看的版本。 */
export function accountsForRealm(list, realm) {
  return (list || []).filter((account) => !account.realm || account.realm === realm);
}

/** 分页，页码越界时收敛到最后一页。 */
export function paginate(items, page, pageSize = PAGE_SIZE) {
  const total = (items || []).length;
  const pages = Math.max(1, Math.ceil(total / pageSize));
  const requested = Math.floor(Number(page) || 1);
  const current = Math.min(Math.max(1, requested), pages);
  const start = (current - 1) * pageSize;
  return { page: current, pages, total, items: (items || []).slice(start, start + pageSize) };
}

/** 空态类型：整个池子没有账号、别的版本有账号而当前版本没有、筛选后为空。 */
export function emptyKind(allAccounts, visibleAccounts, filteredAccounts) {
  if ((filteredAccounts || []).length) return '';
  if (!(visibleAccounts || []).length) return (allAccounts || []).length ? 'realm' : 'none';
  return 'filtered';
}

/** 每个版本的头部动作：签到只对国内版出现，两种打卡只对国际版出现。 */
export function realmActions(realm) {
  return {
    checkin: realm === 'cn',
    dailyChat: realm === 'intl',
    dailyChatWeb: realm === 'intl',
  };
}

/** 积分数据的新鲜度文案。 */
export function creditAgeText(credits, now = Date.now()) {
  const ms = toMillis(credits && credits.updated_at);
  if (ms === null) return '';
  if ((now - ms) / 1000 < 60) return '刚刚更新';
  return '更新于' + fmtAgo(ms, now);
}

/** 套餐到期倒计时文案。 */
export function expiryCountdown(msLeft) {
  if (!Number.isFinite(msLeft)) return '—';
  if (msLeft <= 0) return '已过期';
  if (msLeft < 3600000) return Math.max(1, Math.floor(msLeft / 60000)) + ' 分钟后到期';
  if (msLeft < 86400000) return (msLeft / 3600000).toFixed(1) + ' 小时后到期';
  return Math.floor(msLeft / 86400000) + ' 天后到期';
}

/** 到期紧迫度：1 天内危险，7 天内警告，其余常规。 */
export function urgencyTone(msLeft) {
  if (!Number.isFinite(msLeft) || msLeft < 86400000) return 'danger';
  if (msLeft < 7 * 86400000) return 'warning';
  return 'muted';
}

/** 归一化 creditExpiries：只保留还有额度且能解析出到期时刻的条目，按到期升序。 */
export function creditExpiries(account) {
  const list = Array.isArray(account && account.creditExpiries) ? account.creditExpiries : [];
  return list
    .map((item) => ({
      at: seconds(item && item.at) * 1000,
      amount: Number((item && item.amount) || 0),
      name: String((item && item.name) || '套餐'),
    }))
    .filter((item) => item.at > 0 && item.amount > 0)
    .sort((x, y) => x.at - y.at);
}

/** 最近一笔到期的角标：额度加倒计时，说明里列出全部套餐。 */
export function creditExpiryChip(account, now = Date.now()) {
  const list = creditExpiries(account);
  if (!list.length) return null;
  const first = list[0];
  const msLeft = first.at - now;
  const lines = list.map((item) => item.name + ' ' + fmtNumber(item.amount) + ' 积分 ' + expiryCountdown(item.at - now));
  const total = list.reduce((sum, item) => sum + item.amount, 0);
  return {
    text: fmtNumber(first.amount) + ' 积分 · ' + expiryCountdown(msLeft),
    tone: urgencyTone(msLeft),
    title: ['共有 ' + fmtNumber(total) + ' 积分带到期时间', ...lines].join('\n'),
    msLeft,
  };
}

/** 有效期内积分合计：只统计未到期的套餐，用完但未过期的仍计入总额；套餐明细缺失时回退服务端汇总值。 */
export function validCredits(account, now = Date.now()) {
  const credits = (account && account.credits) || null;
  if (!credits) return null;
  const packages = Array.isArray(credits.packages) ? credits.packages : null;
  if (!packages) {
    return { remain: Number(credits.remain), size: Number(credits.size) };
  }
  let remain = 0;
  let size = 0;
  for (const item of packages) {
    if (!item || typeof item !== 'object') continue;
    const at = toMillis(item.expireAt);
    if (at !== null && at <= now) continue;
    const packRemain = Number(item.remain);
    const packSize = Number(item.size);
    if (Number.isFinite(packRemain)) remain += packRemain;
    if (Number.isFinite(packSize)) size += packSize;
  }
  return { remain, size };
}

/** 积分剩余比例：有效期内可用占总额的百分比；总额未知或为 0 时返回 null，调用方不画进度条。 */
export function creditBarPct(account, now = Date.now()) {
  const valid = validCredits(account, now);
  if (!valid || !(valid.size > 0) || !Number.isFinite(valid.remain)) return null;
  return Math.max(0, Math.min(100, (valid.remain / valid.size) * 100));
}

/** 令牌有效期进度：剩余秒数、百分比与色调；进度以签发到到期的实际时长为满格，拿不到签发时间则 pct 为 null。 */
export function tokenExpiryView(account, now = Date.now()) {
  if (account && account.apiKey) {
    return { remain: null, text: '长期有效', pct: null, tone: 'success' };
  }
  const ms = toMillis(account && account.expiresAt);
  if (ms === null) {
    return { remain: null, text: String((account && account.expiresIn) || '未知'), pct: 0, tone: 'muted' };
  }
  const remain = Math.floor((ms - now) / 1000);
  const issued = toMillis(account && account.issuedAt);
  const span = issued === null ? 0 : Math.floor((ms - issued) / 1000);
  const pct = remain <= 0 ? 0 : span > 0 ? Math.min(100, (remain / span) * 100) : null;
  const tone = remain <= 0 ? 'danger' : remain < 86400 ? 'danger' : remain < 7 * 86400 ? 'warning' : 'success';
  return { remain, text: remainText(remain), pct, tone };
}

function remainText(seconds) {
  if (seconds <= 0) return '已过期';
  if (seconds < 3600) return Math.max(1, Math.floor(seconds / 60)) + ' 分钟';
  if (seconds < 86400) return (seconds / 3600).toFixed(1) + ' 小时';
  return (seconds / 86400).toFixed(1) + ' 天';
}

/** 优先级输入框的校验，返回提交值或写给用户的原因。 */
export function parsePriorityInput(raw, limits) {
  const max = accountLimits(limits).priorityMax;
  const text = String(raw == null ? '' : raw).trim();
  const value = text === '' ? Number.NaN : Number(text);
  if (!Number.isInteger(value) || value < 0 || value > max) {
    return { ok: false, message: '优先级需要 0 到 ' + max + ' 之间的整数，本次没有发送请求' };
  }
  return { ok: true, value };
}

/** 心跳重绘时保留正在编辑的输入内容：命中草稿才返回草稿文本。 */
export function draftFor(draft, uid, field) {
  if (!draft || draft.uid !== String(uid || '') || draft.field !== field) return null;
  return draft.value;
}

/** data-uid 只保留安全字符。 */
export function uidAttr(uid) {
  return String(uid || '').replace(/[^\w.@-]/g, '');
}

/** 出口下拉的选项与当前取值。账号上还有旧的单账号代理时优先展示它。 */
export function slotOptions(account, slots) {
  const current = String((account && account.proxySlot) || '');
  const legacy = !current && account && account.proxy ? String(account.proxy) : '';
  const options = [];
  if (legacy) {
    options.push({ value: '__legacy__', label: '旧代理：' + (legacy.length > 28 ? legacy.slice(0, 25) + '...' : legacy) });
    options.push({ value: '', label: '直连，并清除旧代理' });
  } else {
    options.push({ value: '', label: '直连' });
  }
  const list = slots || [];
  for (const slot of list) {
    const id = String(slot.id || '');
    if (!id) continue;
    options.push({ value: id, label: (slot.name || id) + (slot.enabled === false ? ' · 已停用' : '') });
  }
  let selected = legacy ? '__legacy__' : current;
  if (selected && !options.some((option) => option.value === selected)) {
    options.push({ value: selected, label: '未知槽位 ' + selected });
  }
  return { options, selected, legacy };
}

/** 身份下拉的选项与当前取值。 */
export function productOptions(account) {
  const current = String((account && account.product) || 'workbuddy');
  return {
    selected: PRODUCTS.some((item) => item.id === current) ? current : 'workbuddy',
    options: PRODUCTS.map((item) => ({ value: item.id, label: item.short + ' · ' + item.label })),
  };
}

/** 身份切换确认弹窗里的称呼。 */
export function productLabel(id) {
  const hit = PRODUCTS.find((item) => item.id === id);
  return hit ? hit.label : String(id);
}

/** 模型排除对话框的初始勾选：名单里的模型默认勾选，已排除的不勾。 */
export function modelSelection(modelIds, denied) {
  const deniedSet = new Set((denied || []).map((id) => String(id).toLowerCase()));
  const ids = (modelIds || []).map((id) => String(id));
  return { ids, picked: new Set(ids.filter((id) => !deniedSet.has(id.toLowerCase()))) };
}

/** 保存时要提交的排除名单：所有没有勾选的模型。 */
export function deniedModels(modelIds, picked) {
  const chosen = new Set([...picked].map((id) => String(id)));
  return (modelIds || []).map((id) => String(id)).filter((id) => !chosen.has(id));
}

/** 账号的行内提示：排除名单的标题。 */
export function disabledModelsTitle(account) {
  const ids = (account && account.disabledModels) || [];
  return ids.length ? '不派给这个账号的模型：' + ids.join('、') : '';
}

/** 账号的累计用量，没有记录时返回零值。 */
export function usageFor(usageMap, uid) {
  const row = (usageMap || {})[uid];
  return {
    requests: Number((row && row.requests) || 0),
    total_tokens: Number((row && row.total_tokens) || 0),
  };
}

/** 批量操作结果汇总成一句话，写清每个账号的结果。 */
export function summarizeResults(results, { okText = '成功', failText = '失败', withDetail = '' } = {}) {
  return (results || []).map((row) => {
    const name = row.nickname || String(row.uid || '').slice(0, 8) || '账号';
    if (row.ok) {
      const detail = withDetail && row[withDetail] ? okText + ' ' + row[withDetail] : okText;
      return name + ': ' + detail;
    }
    return name + ': ' + String(row.error || row.msg || failText);
  }).join('；');
}

export function anyOk(results) {
  return (results || []).some((row) => row && row.ok);
}

/** 还没查到积分的账号：每个账号在一次页面生命周期里只补一次。 */
export function creditsToFill(accounts, tried) {
  return (accounts || [])
    .filter((account) => account.uid && !account.credits && !tried.has(account.uid))
    .map((account) => account.uid);
}

/** 把积分查询结果写回账号列表，其它字段保持服务端最新值。 */
export function mergeCredits(accounts, results) {
  const byUid = new Map((results || []).filter((row) => row && row.uid).map((row) => [row.uid, row]));
  return (accounts || []).map((account) => {
    const hit = byUid.get(account.uid);
    return hit ? { ...account, credits: hit.credits || account.credits } : account;
  });
}

/** 出口分配计划：只为已启用且未绑定出口的账号轮流分配已启用的槽位，已有绑定的保持不变。 */
export function autoAssignPlan(accounts, slots) {
  const unbound = (accounts || []).filter((account) => account.enabled && !account.proxySlot);
  if (!unbound.length) return { rows: [], instead: '所有已启用账号都已绑定代理出口，本次没有改动' };
  const usable = (slots || []).filter((slot) => slot.enabled !== false && slot.id);
  if (!usable.length) return { rows: [], instead: '没有已启用的代理槽，请先在「设置 → 代理槽」添加并保存' };
  return {
    rows: unbound.map((account, index) => ({
      uid: account.uid,
      nickname: account.nickname,
      slotId: usable[index % usable.length].id,
    })),
    instead: '',
  };
}

/** 停用确认弹窗的说明文案，写清影响范围。 */
export function disableAllText() {
  return '会把账号池里全部账号一起停用，包含国内版与国际版。停用后网关不会再把请求发给它们，签到与保活也会跳过；随时可以再点「全部启用」恢复。';
}
