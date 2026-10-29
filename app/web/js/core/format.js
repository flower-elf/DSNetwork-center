// 数字与时间的显示格式，全部是纯函数，可以直接在 Node 里测试。

/** 秒或毫秒时间戳统一成毫秒；无效值返回 null。 */
export function toMillis(ts) {
  const n = Number(ts);
  if (!Number.isFinite(n) || n <= 0) return null;
  return n > 1e12 ? n : n * 1000;
}

const pad2 = (n) => String(n).padStart(2, '0');

// 表格一轮会格式化上千个数字，复用格式化器比每次 toLocaleString 少建临时对象。
const numberFormat = new Intl.NumberFormat('zh-CN');
const moneyFormat = new Intl.NumberFormat('zh-CN', {
  minimumFractionDigits: 2, maximumFractionDigits: 2,
});

export function fmtNumber(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '0';
  return numberFormat.format(n);
}

/** 1234 → 1.2k，56789 → 57k，3400000 → 3.4M。 */
export function fmtCompact(v) {
  const n = Number(v) || 0;
  const abs = Math.abs(n);
  if (abs < 1000) return String(Math.round(n));
  if (abs < 1e6) return (abs < 10000 ? (n / 1000).toFixed(1) : String(Math.round(n / 1000))) + 'k';
  return (n / 1e6).toFixed(1) + 'M';
}

/** 积分：整百以上取整并加千分位，1 以上两位小数，更小的保留四位。 */
export function fmtCredit(v) {
  const n = Number(v);
  if (!Number.isFinite(n) || n === 0) return '0';
  const abs = Math.abs(n);
  if (abs >= 100) return numberFormat.format(Math.round(n));
  if (abs >= 1) return n.toFixed(2);
  return n.toFixed(4);
}

/** 金额：人民币符号加两位小数。 */
export function fmtCny(v) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return '¥' + moneyFormat.format(n);
}

export function fmtLatency(ms) {
  const n = Number(ms);
  if (!Number.isFinite(n) || n <= 0) return '—';
  return n < 1000 ? Math.round(n) + 'ms' : (n / 1000).toFixed(2) + 's';
}

export function fmtPercent(v, digits = 1) {
  const n = Number(v);
  if (!Number.isFinite(n)) return '—';
  return n.toFixed(digits) + '%';
}

export function fmtDateTime(ts) {
  const ms = toMillis(ts);
  if (ms === null) return '—';
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
}

export function fmtDate(ts) {
  const ms = toMillis(ts);
  if (ms === null) return '—';
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

/** 今天只显示时刻，昨天加「昨天」，更早显示月日。按浏览器本地日历日判断。 */
export function fmtDateTimeMarked(ts, now = Date.now()) {
  const ms = toMillis(ts);
  if (ms === null) return '—';
  const d = new Date(ms);
  const time = `${pad2(d.getHours())}:${pad2(d.getMinutes())}:${pad2(d.getSeconds())}`;
  const today = new Date(now);
  const dayIndex = (x) => Date.UTC(x.getFullYear(), x.getMonth(), x.getDate()) / 86400000;
  const diff = dayIndex(today) - dayIndex(d);
  if (diff === 0) return time;
  if (diff === 1) return '昨天 ' + time;
  return `${pad2(d.getMonth() + 1)}-${pad2(d.getDate())} ${time}`;
}

/** 本地时间「月-日 时:分」。 */
export function fmtClock(ms) {
  const d = new Date(Number(ms));
  if (!Number.isFinite(d.getTime())) return '—';
  return pad2(d.getMonth() + 1) + '-' + pad2(d.getDate()) + ' ' + pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

/** 本地时间「时:分」。 */
export function fmtTime(ms) {
  const d = new Date(Number(ms));
  if (!Number.isFinite(d.getTime())) return '—';
  return pad2(d.getHours()) + ':' + pad2(d.getMinutes());
}

/** 数字或破折号：空值与非数字都显示破折号。 */
export function fmtNumberDash(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value);
  return Number.isFinite(n) ? fmtNumber(n) : '—';
}

/** 剩余秒数 → 「已过期」「35 分钟」「5.5 小时」「12.3 天」。 */
export function fmtRemain(seconds) {
  const s = Number(seconds);
  if (!Number.isFinite(s)) return '—';
  if (s <= 0) return '已过期';
  if (s < 3600) return Math.max(1, Math.floor(s / 60)) + ' 分钟';
  if (s < 86400) return (s / 3600).toFixed(1) + ' 小时';
  return (s / 86400).toFixed(1) + ' 天';
}

export function fmtAgo(ts, now = Date.now()) {
  const ms = toMillis(ts);
  if (ms === null) return '从未';
  const diff = (now - ms) / 1000;
  if (diff < 1) return '刚刚';
  if (diff < 60) return Math.floor(diff) + ' 秒前';
  if (diff < 3600) return Math.floor(diff / 60) + ' 分钟前';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时前';
  return Math.floor(diff / 86400) + ' 天前';
}

/** 未来时刻 → 「3 天后」「5 小时后」「12 分钟后」，已过去返回「已到期」。 */
export function fmtUntil(ts, now = Date.now()) {
  const ms = toMillis(ts);
  if (ms === null) return '—';
  const diff = (ms - now) / 1000;
  if (diff <= 0) return '已到期';
  if (diff < 3600) return Math.max(1, Math.floor(diff / 60)) + ' 分钟后';
  if (diff < 86400) return Math.floor(diff / 3600) + ' 小时后';
  return Math.floor(diff / 86400) + ' 天后';
}

/** datetime-local 输入框的值与秒级时间戳互转，按浏览器本地时区。 */
export function toLocalInputValue(epochSec) {
  const ms = toMillis(epochSec);
  if (ms === null) return '';
  const d = new Date(ms);
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}T${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

export function fromLocalInputValue(value) {
  if (!value) return null;
  const t = new Date(value).getTime();
  return Number.isNaN(t) ? null : Math.floor(t / 1000);
}

export function realmLabel(realm) {
  return realm === 'cn' ? '国内版' : '国际版';
}

/** 进度条填充颜色：超过半数绿色，剩余不到一半黄色，再少时红色。 */
export function barColor(pct) {
  const value = Number(pct);
  if (!Number.isFinite(value)) return 'var(--danger)';
  if (value > 50) return 'var(--success)';
  if (value >= 25) return 'var(--warning)';
  return 'var(--danger)';
}

/** 排序比较器固定按中文习惯，不跟随运行环境的语言设置。 */
const nameCollator = new Intl.Collator('zh-CN');

export function compareText(a, b) {
  return nameCollator.compare(String(a), String(b));
}
