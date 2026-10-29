// 密钥页的纯逻辑：状态判定、表单校验、请求体组装与客户端接入片段；界面状态与请求在 keys.js / keys/dialogs.js。
import { fmtNumber, fmtCredit, fmtDate, fmtAgo, fmtUntil } from '../../core/format.js';

const KEY_STATUSES = ['ok', 'disabled', 'expired', 'quota_exceeded'];

export const STATUS_TEXT = {
  ok: '正常',
  disabled: '已停用',
  expired: '已过期',
  quota_exceeded: '超配额',
};

export const STATUS_TONE = {
  ok: 'success',
  disabled: 'muted',
  expired: 'warning',
  quota_exceeded: 'danger',
};

export const STATUS_HINT = {
  ok: '密钥可用',
  disabled: '密钥已被停用，调用会收到 401',
  expired: '已超过有效期，调用会收到 401',
  quota_exceeded: 'Token 或积分用量达到配额，调用会收到 429',
};

export const REALM_CHOICES = [
  { value: '', label: '跟随面板默认出口', short: '跟随面板默认出口' },
  { value: 'intl', label: '国际版', short: '国际版' },
  { value: 'cn', label: '国内版', short: '国内版' },
];

export const EXPIRY_MODES_NEW = [
  { value: 'never', label: '永不过期' },
  { value: 'days', label: '指定天数' },
];

export const EXPIRY_MODES_EDIT = [
  { value: 'keep', label: '保持不变' },
  { value: 'days', label: '从现在起 N 天' },
  { value: 'never', label: '永不过期' },
];

/** 服务端返回的用量块，字段缺失时按 0 处理。 */
function keyUsage(row) {
  const usage = (row && row.usage) || {};
  return {
    requests: Number(usage.requests) || 0,
    totalTokens: Number(usage.total_tokens) || 0,
    credit: Number(usage.credit) || 0,
    lastUsedAt: Number(usage.last_used_at) || 0,
  };
}

function quotaTokens(row) {
  return Math.max(0, Number(row && row.quota_tokens) || 0);
}

function quotaCredit(row) {
  return Math.max(0, Number(row && row.quota_credit) || 0);
}

/** 用量达到任一已设置的配额即为 true。 */
export function quotaExceeded(row) {
  const usage = keyUsage(row);
  const tokens = quotaTokens(row);
  if (tokens > 0 && usage.totalTokens >= tokens) return true;
  const credit = quotaCredit(row);
  return credit > 0 && usage.credit >= credit;
}

/** 状态以服务端的 status 为准；缺失时按同一套口径推导，避免把已过期或超配额的密钥画成正常。 */
export function deriveKeyStatus(row, nowMs = Date.now()) {
  if (row && KEY_STATUSES.includes(row.status)) return row.status;
  if (!row || row.enabled === false) return 'disabled';
  const expiresAt = Number(row.expires_at) || 0;
  if (expiresAt > 0 && nowMs >= expiresAt * 1000) return 'expired';
  if (quotaExceeded(row)) return 'quota_exceeded';
  return 'ok';
}

export function statusText(row, nowMs) {
  return STATUS_TEXT[deriveKeyStatus(row, nowMs)];
}

export function realmBindingText(realm) {
  const hit = REALM_CHOICES.find((c) => c.value === realm);
  return hit ? hit.short : REALM_CHOICES[0].short;
}

/** 列表里显示的掩码；服务端只回掩码，明文要用 /settings/reveal 单独读取。 */
export function keyDisplay(row) {
  const masked = String((row && row.masked) || '').trim();
  return masked || '未设置';
}

export function expiryText(row, nowMs = Date.now()) {
  const expiresAt = Number(row && row.expires_at) || 0;
  if (!expiresAt) return '永不过期';
  const date = fmtDate(expiresAt);
  return nowMs >= expiresAt * 1000 ? date + ' 已过期' : date;
}

export function expiryHint(row, nowMs = Date.now()) {
  const expiresAt = Number(row && row.expires_at) || 0;
  if (!expiresAt) return '永不过期';
  return fmtUntil(expiresAt, nowMs);
}

function ipList(row) {
  const list = row && row.ip_allowlist;
  return Array.isArray(list) ? list.filter((v) => String(v || '').trim()) : [];
}

function modelPatterns(row) {
  const list = row && row.models;
  return Array.isArray(list) ? list.filter((v) => String(v || '').trim()) : [];
}

export function ipLimitText(row) {
  const list = ipList(row);
  return list.length ? `白名单 ${list.length} 条` : '不限 IP';
}

export function modelLimitText(row) {
  const list = modelPatterns(row);
  return list.length ? `限 ${list.length} 个模型` : '全部模型';
}

/** 限制单元格的悬停明细。 */
export function limitTitle(row) {
  const ips = ipList(row);
  const models = modelPatterns(row);
  const parts = [];
  parts.push(ips.length ? 'IP 白名单: ' + ips.join(', ') : 'IP 白名单: 不限');
  parts.push(models.length ? '模型限制: ' + models.join(', ') : '模型限制: 全部模型');
  return parts.join('\n');
}

/** 已用一行：Token 与积分；设置了配额时补充配额上限。 */
export function usageLines(row) {
  const usage = keyUsage(row);
  const tokens = quotaTokens(row);
  const credit = quotaCredit(row);
  const first = `${fmtNumber(usage.totalTokens)} token · ${fmtCredit(usage.credit)} 积分`;
  if (!tokens && !credit) return [first, ''];
  const quotaParts = [];
  if (tokens) quotaParts.push(`${fmtNumber(tokens)} token`);
  if (credit) quotaParts.push(`${fmtCredit(credit)} 积分`);
  return [first, `配额 ${quotaParts.join(' · ')}`];
}

export function lastUsedText(row, nowMs = Date.now()) {
  const usage = keyUsage(row);
  return usage.lastUsedAt ? fmtAgo(usage.lastUsedAt, nowMs) : '从未使用';
}

export function summarizeKeys(rows, nowMs = Date.now()) {
  const out = { total: 0, ok: 0, disabled: 0, expired: 0, quota_exceeded: 0 };
  for (const row of rows || []) {
    out.total += 1;
    out[deriveKeyStatus(row, nowMs)] += 1;
  }
  return out;
}

/**
 * 认证状态说明，返回 { kind, text }：kind 为 none 表示不校验 Key，
 * startup 表示由启动参数把关，panel 表示由本页的密钥列表把关。
 */
export function authSummary(view) {
  if (view && view.auth_required === false) {
    return { kind: 'none', text: '当前不校验 Key：/v1 接口允许匿名调用，任何客户端都能直接使用网关。' };
  }
  const rows = (view && view.api_keys) || [];
  // 服务端只要有一把「启用」的密钥就要求带 Key，过期或超配额的那些同样把门。
  const enabled = rows.filter((row) => row && row.enabled !== false).length;
  // 列表里一旦有密钥，接口只认这些密钥，启动参数里那把会被忽略。
  if (enabled) return { kind: 'panel', text: `已启用 ${enabled} 个密钥，接口会进行鉴权。` };
  if (rows.length) {
    return view && view.api_key_set
      ? { kind: 'locked', text: '密钥列表里的密钥都已停用，但接口仍要求携带密钥：启动参数里的 Key 已不再被接受，需要启用列表里的某一把。' }
      : { kind: 'open', text: '密钥列表里的密钥都已停用，/v1 接口当前不校验 Key。' };
  }
  if (view && view.api_key_set && view.api_key_set_by_panel === false) {
    return { kind: 'startup', text: `认证由启动参数里的 Key 把关：${view.api_key_masked || ''}` };
  }
  if (view && view.api_key_set) {
    return { kind: 'legacy', text: `认证由设置文件里保存的单个 Key 把关：${view.api_key_masked || ''}` };
  }
  return { kind: 'open', text: '没有任何生效的密钥，/v1 接口当前不校验 Key。' };
}

/** 启动参数里配置的 Key 只在运行时存在，面板不能编辑，这里整理成一行只读信息。 */
export function startupKeyRow(view) {
  if (!view || !view.api_key_set || view.api_key_set_by_panel === true) return null;
  return {
    name: '启动参数 Key',
    masked: view.api_key_masked || '已配置',
    note: (view.api_keys || []).length
      ? '来自启动参数；密钥列表里已经有密钥，接口只认列表里的那些，这把 Key 已不再生效。'
      : '来自启动参数，面板不能修改；设置端口时传入，重启网关后生效。',
  };
}

// ---------------------------------------------------------------------------
// 模型限制：标签编辑器
// ---------------------------------------------------------------------------

/** 把输入整理成小写、去重、去掉空白的模型名模式；支持逗号、分号与空格分隔。 */
export function parsePatternInput(raw) {
  const out = [];
  for (const part of String(raw == null ? '' : raw).split(/[,;\s]+/)) {
    const pattern = part.trim().toLowerCase();
    if (pattern && !out.includes(pattern)) out.push(pattern);
  }
  return out;
}

export function addPatterns(list, raw) {
  const out = Array.isArray(list) ? list.slice() : [];
  for (const pattern of parsePatternInput(raw)) {
    if (!out.includes(pattern)) out.push(pattern);
  }
  return out;
}

export function removePatternAt(list, index) {
  const out = Array.isArray(list) ? list.slice() : [];
  if (index >= 0 && index < out.length) out.splice(index, 1);
  return out;
}

// ---------------------------------------------------------------------------
// IP 白名单校验
// ---------------------------------------------------------------------------

function isIpv4(value) {
  const parts = value.split('.');
  if (parts.length !== 4) return false;
  return parts.every((part) => /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)$/.test(part));
}

function isIpv6(value) {
  // 压缩写法允许出现一次「::」；逐段校验 1 到 4 位十六进制。
  const halves = value.split('::');
  if (halves.length > 2) return false;
  const groups = [];
  for (const half of halves) {
    if (half === '') continue;
    for (const group of half.split(':')) {
      if (!/^[0-9a-fA-F]{1,4}$/.test(group)) return false;
      groups.push(group);
    }
  }
  if (!groups.length) return false;
  return halves.length === 2 ? groups.length <= 7 : groups.length === 8;
}

/** 单个 IP 或 CIDR 是否合法；IPv4 支持 /0-32，IPv6 支持 /0-128。 */
export function isIpOrCidr(entry) {
  const text = String(entry == null ? '' : entry).trim();
  if (!text) return false;
  const slash = text.indexOf('/');
  const base = slash === -1 ? text : text.slice(0, slash);
  const prefix = slash === -1 ? '' : text.slice(slash + 1);
  const ipv6 = base.includes(':');
  if (!(ipv6 ? isIpv6(base) : isIpv4(base))) return false;
  if (slash === -1) return true;
  if (!/^\d{1,3}$/.test(prefix)) return false;
  const bits = Number(prefix);
  return ipv6 ? bits <= 128 : bits <= 32;
}

/** 多行文本 → { ok, list, invalid }；list 按输入顺序去重。 */
export function parseIpAllowlist(text) {
  const list = [];
  const invalid = [];
  for (const raw of String(text == null ? '' : text).split(/[\s,;]+/)) {
    const entry = raw.trim();
    if (!entry) continue;
    if (!isIpOrCidr(entry)) {
      if (!invalid.includes(entry)) invalid.push(entry);
      continue;
    }
    if (!list.includes(entry)) list.push(entry);
  }
  return { ok: invalid.length === 0, list, invalid };
}

// ---------------------------------------------------------------------------
// 新建 / 编辑表单
// ---------------------------------------------------------------------------

function expiryDaysFromRow(row, nowMs = Date.now()) {
  const expiresAt = Number(row && row.expires_at) || 0;
  if (!expiresAt) return 30;
  const days = Math.ceil((expiresAt * 1000 - nowMs) / 86400000);
  return days > 0 ? days : 1;
}

/** 新建或编辑时使用的表单初值。 */
export function formFromRow(row, nowMs = Date.now()) {
  const usage = keyUsage(row);
  const editing = !!(row && row.id);
  return {
    id: (row && row.id) || '',
    editing,
    name: (row && row.name) || '',
    enabled: !(row && row.enabled === false),
    keyValue: '',
    masked: (row && row.masked) || '',
    realm: (row && row.realm) || '',
    patterns: modelPatterns(row),
    patternsKnown: !!(row && Array.isArray(row.models)),
    patternsTouched: false,
    expiryMode: editing ? 'keep' : 'never',
    expiryDays: expiryDaysFromRow(row, nowMs),
    quotaTokens: quotaTokens(row) ? String(quotaTokens(row)) : '',
    quotaCredit: quotaCredit(row) ? String(quotaCredit(row)) : '',
    ipText: ipList(row).join('\n'),
    usageStart: Number(row && row.usage_reset_at) || 0,
    usedTokens: usage.totalTokens,
    usedCredit: usage.credit,
  };
}

const MAX_EXPIRY_DAYS = 3650;

/**
 * 表单校验：返回错误文案，空串表示通过。
 * 口径与服务端一致：密钥内容至少 4 个字符，有效期天数与配额是非负整数。
 */
export function validateKeyForm(form) {
  const value = String(form.keyValue || '').trim();
  if (!form.editing && !value) return '请填写密钥内容，或点击「随机生成」生成一个。';
  if (value && value.length < 4) return '密钥内容至少 4 个字符。';
  if (!['', 'intl', 'cn'].includes(form.realm || '')) return '出口绑定的取值不合法。';
  if (form.expiryMode === 'days') {
    const days = Number(form.expiryDays);
    if (!Number.isInteger(days) || days < 1) return '有效期天数必须是大于等于 1 的整数。';
    if (days > MAX_EXPIRY_DAYS) return `有效期天数最多 ${MAX_EXPIRY_DAYS} 天。`;
  }
  const tokens = String(form.quotaTokens == null ? '' : form.quotaTokens).trim();
  if (tokens !== '') {
    const n = Number(tokens);
    if (!Number.isInteger(n) || n < 0) return '配额 Token 必须是 0 或正整数。';
  }
  const credit = String(form.quotaCredit == null ? '' : form.quotaCredit).trim();
  if (credit !== '') {
    const n = Number(credit);
    if (!Number.isFinite(n) || n < 0) return '配额积分必须是 0 或正数。';
  }
  const allowlist = parseIpAllowlist(form.ipText);
  if (!allowlist.ok) return 'IP 白名单里有不合法的条目：' + allowlist.invalid.join(', ');
  return '';
}

/** 表单 → 要提交给服务端的密钥条目；未涉及的字段不写，服务端保留原值。 */
export function buildKeyEntry(form, nowMs = Date.now()) {
  const patterns = Array.isArray(form.patterns) ? form.patterns.slice() : [];
  const entry = {
    id: form.id || '',
    name: String(form.name || '').trim() || '未命名',
    key: String(form.keyValue || '').trim(),
    realm: form.realm || '',
    enabled: form.enabled !== false,
  };
  // 服务端旧版本不回 models 字段：只有确实改过或本来就拿到过才提交，避免把已存的模型限制清空。
  if (form.patternsKnown || form.patternsTouched || patterns.length) entry.models = patterns;
  if (form.expiryMode === 'never') entry.expires_at = 0;
  if (form.expiryMode === 'days') entry.expires_at = Math.floor(nowMs / 1000) + Number(form.expiryDays) * 86400;
  // 「保持不变」不带 expires_at，服务端保留原值。
  entry.quota_tokens = Math.max(0, Math.floor(Number(form.quotaTokens) || 0));
  entry.quota_credit = Math.max(0, Number(form.quotaCredit) || 0);
  entry.ip_allowlist = parseIpAllowlist(form.ipText).list;
  return entry;
}

/**
 * 服务端返回的行 → 提交载荷：旧字段照旧提交，新字段存在时才带，
 * 避免后端还没实现新字段时把已有值覆盖成空。
 */
export function keyRowPayload(row) {
  const payload = {
    id: (row && row.id) || '',
    name: (row && row.name) || '',
    realm: (row && row.realm) || '',
    enabled: !(row && row.enabled === false),
    key: String((row && row.key) || '').trim(),
  };
  if (row && Array.isArray(row.models)) payload.models = modelPatterns(row);
  if (row && 'expires_at' in row) payload.expires_at = Number(row.expires_at) || 0;
  if (row && 'quota_tokens' in row) payload.quota_tokens = Math.max(0, Number(row.quota_tokens) || 0);
  if (row && 'quota_credit' in row) payload.quota_credit = Math.max(0, Number(row.quota_credit) || 0);
  if (row && 'ip_allowlist' in row) payload.ip_allowlist = ipList(row);
  return payload;
}

export function buildSavePayload(rows) {
  // 显式空删除列表 = upsert：这份列表只增改自己带来的行，不会把另一个标签页
  // 新建、而本地尚未刷新到的密钥当成“没提交”删掉。删除操作另外附带 id。
  return { api_keys: (rows || []).map(keyRowPayload), deleted_api_key_ids: [] };
}

/** 本地行列表 → 逐行提交载荷，供开关、删除等操作复用。 */
export function rowsAfterReplace(rows, index, entry) {
  const out = (rows || []).map(keyRowPayload);
  if (!entry) {
    out.splice(index, 1);
    return out;
  }
  if (index < 0 || index >= out.length) out.push(keyRowPayload(entry));
  else out[index] = keyRowPayload(entry);
  return out;
}

// ---------------------------------------------------------------------------
// 创建成功后的客户端接入片段
// ---------------------------------------------------------------------------

export function apiBaseUrl(origin) {
  const base = String(origin || '').replace(/\/+$/, '');
  return base + '/v1';
}

/** hub 把 Messages 接口挂在 /v1/messages，客户端会自行追加 /v1，所以基址不带 /v1。 */
export function anthropicBaseUrl(origin) {
  return String(origin || '').replace(/\/+$/, '');
}

export function clientSnippets(origin, key, model = 'deepseek-v4.1-flash') {
  const base = apiBaseUrl(origin);
  const body = JSON.stringify({ model, messages: [{ role: 'user', content: '你好' }] });
  return [
    {
      id: 'curl',
      title: 'curl 示例',
      hint: '直接调用对话补全接口，验证密钥是否可用。',
      text: `curl ${base}/chat/completions \\\n  -H "Authorization: Bearer ${key}" \\\n  -H "Content-Type: application/json" \\\n  -d '${body}'`,
    },
    {
      id: 'openai',
      title: 'OpenAI 兼容环境变量',
      hint: 'OpenAI SDK、Codex 等客户端读取这两个环境变量。',
      text: `export OPENAI_BASE_URL=${base}\nexport OPENAI_API_KEY=${key}`,
    },
    {
      id: 'anthropic',
      title: 'Claude Code 环境变量',
      hint: 'Claude Code 会在基址后追加 /v1/messages，所以基址不带 /v1。',
      text: `export ANTHROPIC_BASE_URL=${anthropicBaseUrl(origin)}\nexport ANTHROPIC_AUTH_TOKEN=${key}`,
    },
  ];
}

/** 新建对话框里点「随机生成」时使用的值：18 字节随机数的十六进制写法。 */
export function randomKeyValue() {
  const bytes = new Uint8Array(18);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (b) => b.toString(16).padStart(2, '0')).join('');
}
