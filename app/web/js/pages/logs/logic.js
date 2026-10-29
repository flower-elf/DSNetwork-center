// 运行日志页的纯逻辑：筛选、合并去重、统计与文本导出；页面模板放在 logs.js 里，这一份不依赖浏览器，方便在 Node 里直接测试。

export const LOG_POLL_MS = 2000;
export const LOG_RESET_LIMIT = 500;
export const LOG_POLL_LIMIT = 200;
export const LOG_MAX = 2000;

export const LEVELS = Object.freeze([
  { id: '', label: '全部级别' },
  { id: 'INFO', label: 'INFO' },
  { id: 'WARN', label: 'WARN' },
  { id: 'ERROR', label: 'ERROR' },
]);

export const TAGS = Object.freeze([
  { id: '', label: '全部模块' },
  { id: 'chat', label: '对话' },
  { id: 'tasks', label: '任务福利' },
  { id: 'scheduler', label: '调度器' },
  { id: 'accounts', label: '账号' },
  { id: 'system', label: '系统' },
]);

const KNOWN_LEVELS = new Set(['INFO', 'WARN', 'ERROR', 'DEBUG']);
const KNOWN_TAGS = new Set(['chat', 'tasks', 'scheduler', 'accounts', 'system', 'auth', 'catalog', 'settings']);

export function defaultFilters() {
  return { level: '', tag: '', search: '' };
}

export function buildLogsQuery({ sinceId = 0, limit = LOG_POLL_LIMIT } = {}) {
  const params = new URLSearchParams();
  if (sinceId > 0) params.set('since_id', String(sinceId));
  params.set('limit', String(limit));
  return '/logs?' + params.toString();
}

function normalizeSearch(text) {
  return String(text === undefined || text === null ? '' : text).trim().toLowerCase();
}

/** 级别、模块与关键字三个条件同时满足才显示；关键字匹配内容、模块与时刻。 */
function matchesLogFilter(entry, filters) {
  if (filters.level && entry.level !== filters.level) return false;
  if (filters.tag && entry.tag !== filters.tag) return false;
  const needle = normalizeSearch(filters.search);
  if (needle) {
    const haystack = ((entry.msg || '') + ' ' + (entry.tag || '') + ' ' + (entry.time || '')).toLowerCase();
    if (!haystack.includes(needle)) return false;
  }
  return true;
}

export function filterLogs(entries, filters) {
  return entries.filter((entry) => matchesLogFilter(entry, filters));
}

/** 增量合并：按 id 去重后追加，超出上限时丢掉最旧的记录。 */
export function mergeLogs(existing, incoming) {
  if (!Array.isArray(incoming) || !incoming.length) return existing;
  const seen = new Set(existing.map((entry) => entry.id));
  const merged = existing.slice();
  for (const entry of incoming) {
    if (seen.has(entry.id)) continue;
    seen.add(entry.id);
    merged.push(entry);
  }
  return merged.length > LOG_MAX ? merged.slice(merged.length - LOG_MAX) : merged;
}

/** 网关没给 max_id 时，用收到的最后一条日志推算下一次的起点。 */
export function nextSinceId(entries, previous = 0) {
  let max = Number(previous) || 0;
  for (const entry of entries || []) {
    const id = Number(entry.id);
    if (Number.isFinite(id) && id > max) max = id;
  }
  return max;
}

export function logStats(entries) {
  let errors = 0;
  let warns = 0;
  for (const entry of entries) {
    if (entry.level === 'ERROR') errors += 1;
    else if (entry.level === 'WARN') warns += 1;
  }
  return { total: entries.length, errors, warns };
}

export function levelClass(level) {
  return 'lvl-' + (KNOWN_LEVELS.has(level) ? level : 'INFO');
}

export function tagClass(tag) {
  const name = String(tag || '');
  return 'tag-' + (KNOWN_TAGS.has(name) ? name : 'system');
}

export function formatLogLine(entry) {
  const at = entry.ts || entry.time || '';
  return `[${at}] [${entry.level || 'INFO'}] [${entry.tag || 'system'}] ${entry.msg || ''}`;
}

export function formatLogText(entries) {
  return entries.map(formatLogLine).join('\n');
}
