// 模型页的纯逻辑：目录来源文案、能力判定、筛选排序、统计与勾选后的 disabled_models 计算；只依赖 core/format.js，可在 Node 里直接测试。

const SOURCE_LABELS = {
  server: '本次从服务器获取',
  cache: '服务器上次返回并保存在本地',
  local: '桌面端本地目录文件',
  bundled: '程序内置模型表',
};

export function sourceLabel(source) {
  if (!source) return '未知来源';
  return SOURCE_LABELS[source] || source;
}

export const LONG_CONTEXT_TOKENS = 128000;

export function fmtTokens(value) {
  const n = Number(value);
  if (!Number.isFinite(n) || n <= 0) return '—';
  const k = Math.round(n / 1000);
  return k >= 1000 ? k / 1000 + 'M' : k + 'K';
}

export function hasVision(entry) {
  return !!(entry.supports_vision || entry.vision || entry.multimodal
    || (Array.isArray(entry.input_modalities) && entry.input_modalities.includes('image')));
}

function hasTools(entry) {
  return !!(entry.supports_tool_calls
    || (entry.capabilities && entry.capabilities.tool_calls));
}

export function hasReasoning(entry) {
  return !!(entry.supports_reasoning || entry.reasoning_fixed_effort || entry.always_reasoning
    || (Array.isArray(entry.reasoning_efforts) && entry.reasoning_efforts.length));
}

function contextValues(entry) {
  const values = [];
  if (entry.context_length) values.push(Number(entry.context_length));
  for (const v of entry.context_windows || []) values.push(Number(v));
  return values.filter((v) => Number.isFinite(v) && v > 0);
}

function maxContext(entry) {
  const values = contextValues(entry);
  return values.length ? Math.max(...values) : 0;
}

function isLongContext(entry) {
  return maxContext(entry) >= LONG_CONTEXT_TOKENS;
}

/** 上下文列的文本：优先单个上下文长度，只有窗口列表时用斜杠连接。 */
export function contextText(entry) {
  if (entry.context_length) return fmtTokens(entry.context_length);
  const windows = (entry.context_windows || []).filter((v) => Number(v) > 0);
  return windows.length ? windows.map(fmtTokens).join(' / ') : '—';
}

/** 上下文列与单元格的悬停说明，写精确数字。 */
export function contextTitle(entry) {
  const windows = (entry.context_windows || []).filter((v) => Number(v) > 0);
  if (!windows.length) return '';
  return '可用的上下文长度：' + windows.join(' / ') + ' tokens';
}

export function outputText(entry) {
  const n = Number(entry.max_output_tokens);
  return Number.isFinite(n) && n > 0 ? fmtTokens(n) : '—';
}

/** 思考档位：固定档位优先，其次可选档位，最后是只标出支持推理。 */
export function reasoningLevels(entry) {
  if (entry.reasoning_fixed_effort) return [String(entry.reasoning_fixed_effort).toLowerCase()];
  if (Array.isArray(entry.reasoning_efforts) && entry.reasoning_efforts.length) {
    return entry.reasoning_efforts.map((level) => String(level).toLowerCase());
  }
  if (entry.supports_reasoning || entry.always_reasoning) return ['原生'];
  return [];
}

/** 模型列里的能力徽章，按固定顺序返回；每项 tone 与说明取自真实字段。 */
export function capabilityBadges(entry) {
  const badges = [];
  if (entry.always_reasoning) {
    badges.push({ key: 'always', label: '仅推理', tone: 'violet', title: '这个模型不能关闭思考，每次回答都会先推理' });
  }
  if (hasVision(entry)) badges.push({ key: 'vision', label: '多模态', tone: 'sky', title: '支持图片输入' });
  if (hasTools(entry)) badges.push({ key: 'tools', label: '工具调用', tone: 'muted', title: '支持 function calling' });
  if (hasReasoning(entry) && !entry.always_reasoning) {
    badges.push({ key: 'reasoning', label: '推理', tone: 'violet', title: '支持思考档位' });
  }
  if (entry.is_default) badges.push({ key: 'default', label: '默认模型', tone: 'success', title: '上游把哪个模型设为默认时用到的名字' });
  return badges;
}

/** 消费倍率的显示值；上游给的是 "x0.79" 这样的字符串，也可能是数字。 */
export function creditsValue(entry) {
  if (entry.credits === null || entry.credits === undefined || entry.credits === '') return null;
  const n = Number(String(entry.credits).toLowerCase().replace('x', ''));
  return Number.isFinite(n) ? n : null;
}

/** 积分倍率的标签：标出限时免费与国内版的夜间折扣。 */
export function creditInfo(entry, realm) {
  const raw = entry.credits === null || entry.credits === undefined ? '' : String(entry.credits);
  const label = raw ? (raw.startsWith('x') ? raw.slice(1) + 'x' : raw) : '';
  const value = creditsValue(entry);
  const alwaysFree = entry.id === 'hy3' || entry.id === 'hy4-preview-f'
    || (entry.id === 'deepseek-v4.1-flash' && realm === 'intl');
  if (value === 0 || alwaysFree) {
    return { label: label || '0.00x', note: '', free: true };
  }
  if ((entry.id === 'glm-5.2' || entry.id === 'deepseek-v4-pro') && realm === 'cn') {
    return { label: label || '0.79x', note: '夜间 0.5x', free: false };
  }
  if (!raw) return null;
  return { label, note: '', free: false };
}

/** 模型目录的地址：强制重新获取时带上 refresh=1，网关据此跳过 5 分钟缓存。 */
export function panelModelsUrl(realm, force = false) {
  return '/panel/models?realm=' + encodeURIComponent(realm) + (force ? '&refresh=1' : '');
}

function searchText(entry) {
  return [entry.id, entry.name, entry.description, entry.vendor, ...(entry.tags || [])]
    .filter(Boolean).join(' ').toLowerCase();
}

export const CAPABILITIES = [
  { id: 'all', label: '全部', test: () => true },
  { id: 'reasoning', label: '支持推理', test: hasReasoning },
  { id: 'long', label: '长上下文', test: isLongContext },
  { id: 'vision', label: '多模态', test: hasVision },
  { id: 'tools', label: '工具调用', test: hasTools },
];

export function capabilityLabel(id) {
  const found = CAPABILITIES.find((c) => c.id === id);
  return found ? found.label : '全部';
}

/** 筛选后仍被隐藏的模型：被排除而且没有勾选的，默认不显示。 */
export function hiddenExcludedCount(data) {
  return (data || []).filter((m) => m.excluded && !m.enabled).length;
}

export function hasCredits(data) {
  return (data || []).some((m) => creditsValue(m) !== null);
}

/** 表格要显示的行：先按「被排除且未处理」隐藏，再按搜索词与能力筛选。 */
export function selectModels(data, options = {}) {
  const query = (options.query || '').trim().toLowerCase();
  const capability = CAPABILITIES.find((c) => c.id === options.capability) || CAPABILITIES[0];
  let rows = (data || []).filter((m) => options.showExcluded || !(m.excluded && !m.enabled));
  if (query) rows = rows.filter((m) => searchText(m).includes(query));
  if (capability.id !== 'all') rows = rows.filter((m) => capability.test(m));
  if (options.sort === 'credit') {
    rows = [...rows].sort((a, b) => {
      const av = creditsValue(a);
      const bv = creditsValue(b);
      return (av === null ? Infinity : av) - (bv === null ? Infinity : bv);
    });
  } else if (options.sort === 'context') {
    rows = [...rows].sort((a, b) => maxContext(b) - maxContext(a));
  }
  return rows;
}

/** 统计卡的数据，全部来自目录里真实存在的字段。 */
export function summary(data) {
  const rows = data || [];
  let maxCtx = 0;
  let maxOut = 0;
  for (const m of rows) {
    maxCtx = Math.max(maxCtx, maxContext(m));
    const out = Number(m.max_output_tokens);
    if (Number.isFinite(out)) maxOut = Math.max(maxOut, out);
  }
  return {
    total: rows.length,
    enabled: rows.filter((m) => m.enabled).length,
    reasoning: rows.filter(hasReasoning).length,
    longContext: rows.filter(isLongContext).length,
    maxContext: maxCtx,
    maxOutput: maxOut,
  };
}

/** 勾选状态变化后的一份新数据，不修改传入的数组。 */
export function withEnabled(data, id, enabled) {
  return (data || []).map((m) => (m.id === id ? { ...m, enabled: !!enabled } : m));
}

/** 要写进 disabled_models 的 id 列表：当前没有勾选的模型。 */
export function disabledIds(data) {
  return (data || []).filter((m) => !m.enabled).map((m) => m.id);
}

/** 当前版本的账号，用于模型页的「分账号查看」。 */
export function realmAccounts(accounts, realm) {
  return (accounts || []).filter((a) => (a.realm || '') === realm);
}

/** 账号模型排除名单的小写集合。 */
function accountDeniedSet(account) {
  return new Set(((account && account.disabledModels) || [])
    .map((id) => String(id).toLowerCase()));
}

export function accountCanUse(model, account) {
  const id = String((model && model.id) || '').toLowerCase();
  return !accountDeniedSet(account).has(id);
}

/** 聚合视图的账号覆盖：这个模型在多少个已启用账号里可用。 */
export function coverageFor(model, accounts) {
  const list = (accounts || []).filter((a) => a.enabled !== false);
  const can = list.filter((a) => accountCanUse(model, a)).length;
  return { can, total: list.length, text: list.length ? can + '/' + list.length : '—' };
}

/** 某个账号视图下可用的模型数：全局开启，并且没有被该账号排除。 */
export function accountAvailableCount(data, account) {
  return (data || []).filter((m) => m.enabled && accountCanUse(m, account)).length;
}
