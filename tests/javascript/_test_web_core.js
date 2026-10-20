/* 看板公共模块：存储容错、请求错误处理、查看版本与默认出口的分离、格式化边界、命令面板匹配、路由解析。 */
import assert from 'node:assert/strict';

const storageBlocked = {
  getItem() { throw new Error('SecurityError: storage denied'); },
  setItem() { throw new Error('SecurityError: storage denied'); },
  removeItem() { throw new Error('SecurityError: storage denied'); },
};
function memoryStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
  };
}

// 浏览器全局对象的最小替身：store.js 读地址栏并用 history.replaceState 改写 ?view=
globalThis.location = new URL('http://panel.local/');
globalThis.history = { replaceState: (_s, _t, url) => { globalThis.location = new URL(url, 'http://panel.local'); } };
globalThis.localStorage = storageBlocked;
globalThis.sessionStorage = storageBlocked;

let server = { status: 200, body: {} };
const requests = [];
globalThis.fetch = async (url, init = {}) => {
  requests.push({ url, init });
  const reply = typeof server === 'function' ? await server(url, init) : server;
  const { status, body, headers = {} } = reply;
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return {
    status,
    ok: status >= 200 && status < 300,
    text: async () => text,
    json: async () => JSON.parse(text),
    headers: { get: (name) => headers[name] ?? headers[name.toLowerCase()] ?? null },
  };
};

const storage = await import('../../app/web/js/core/storage.js');
const api = await import('../../app/web/js/core/api.js');
const store = await import('../../app/web/js/core/store.js');
const format = await import('../../app/web/js/core/format.js');
const refresh = await import('../../app/web/js/core/refresh.js');
const palette = await import('../../app/web/js/ui/palette-logic.js');
const router = await import('../../app/web/js/core/router.js');

// ---- 存储被禁用时不抛错，可用时能正常读写
assert.equal(storage.readStore('x'), null);
storage.writeStore('x', '1');
globalThis.localStorage = memoryStorage();
storage.writeStore('wb-test', 'on');
assert.equal(storage.readStore('wb-test'), 'on');
storage.writeStore('wb-test', null);
assert.equal(storage.readStore('wb-test'), null);

// ---- 自动刷新间隔：默认 60 秒，0 表示不自动刷新，空值回默认，最多一小时
assert.equal(refresh.refreshSeconds(), 60, '没设置过时用默认值');
assert.equal(refresh.setRefreshSeconds('30'), 30);
assert.equal(refresh.refreshSeconds(), 30);
assert.equal(refresh.setRefreshSeconds('0'), 0);
assert.equal(refresh.refreshSeconds(), 0, '0 要存下来，不能回到默认');
assert.equal(refresh.setRefreshSeconds(''), 60, '空值回到默认');
assert.equal(refresh.setRefreshSeconds('99999'), 3600, '超过上限按一小时');
assert.equal(refresh.setRefreshSeconds('-5'), 0);
const intervalEvents = [];
const stopIntervals = refresh.onRefreshSeconds((seconds) => intervalEvents.push(seconds));
refresh.setRefreshSeconds(15);
assert.deepEqual(intervalEvents, [15]);
stopIntervals();
refresh.setRefreshSeconds(20);
assert.deepEqual(intervalEvents, [15], '取消订阅后不再收到通知');
refresh.setRefreshSeconds(60);
assert.equal(storage.readStore('wb-refresh-interval'), null, '默认值不写存储');
globalThis.localStorage = storageBlocked;
assert.equal(refresh.refreshSeconds(), 60, '存储被禁用时用默认值');
assert.doesNotThrow(() => refresh.setRefreshSeconds(5));
globalThis.localStorage = memoryStorage();

// ---- 请求错误：错误信息原样带出，401 触发会话失效，非 JSON 正文也有说明
let lost = 0;
api.onUnauthorized(() => { lost += 1; });
server = { status: 400, body: { error: { message: 'current password is wrong' } } };
await assert.rejects(api.postJSON('/panel/password', {}), (err) => err instanceof api.ApiError && err.status === 400
  && err.message === 'current password is wrong' && !api.isAuthError(err));
assert.equal(lost, 0, '400 不应被当作会话失效');
server = { status: 401, body: { error: { message: 'panel password required' } } };
await assert.rejects(api.getJSON('/accounts'), (err) => api.isAuthError(err));
assert.equal(lost, 1);
server = { status: 401, body: { error: { message: 'current password is wrong' } } };
await assert.rejects(api.postJSON('/panel/password', { current: 'x' }),
  (err) => err instanceof api.ApiError && err.status === 401
    && err.message === 'current password is wrong');
assert.equal(lost, 1, '修改密码时的 401 是表单错误，不能登出面板');
server = { status: 403, body: { error: { message: 'realm not allowed' } } };
await assert.rejects(api.postJSON('/settings/save', {}),
  (err) => err instanceof api.ApiError && err.status === 403 && !api.isAuthError(err));
assert.equal(lost, 1, '403 不等于会话失效');
server = { status: 502, body: '<html>Bad Gateway</html>' };
await assert.rejects(api.getJSON('/usage'), (err) => err.status === 502 && err.message.includes('Bad Gateway'));
server = { status: 200, body: { ok: true } };
assert.deepEqual(await api.postJSON('/x', { a: 1 }), { ok: true });
assert.equal(requests.at(-1).init.headers['Content-Type'], 'application/json');
assert.equal(requests.at(-1).init.body, '{"a":1}');

// ---- 条件请求：第二轮带 If-None-Match，304 复用上次解析结果
api.clearResponseCache();
let etagCalls = 0;
server = (_url, init) => {
  etagCalls += 1;
  if (init.headers['If-None-Match'] === '"v1"') {
    return { status: 304, body: '', headers: { ETag: '"v1"' } };
  }
  return { status: 200, body: { value: 1 }, headers: { ETag: '"v1"' } };
};
assert.deepEqual(await api.getJSON('/etag'), { value: 1 });
assert.deepEqual(await api.getJSON('/etag'), { value: 1 }, '304 要交回上次的对象');
assert.equal(etagCalls, 2);
assert.equal(requests.at(-1).init.headers['If-None-Match'], '"v1"');

// ---- 写请求成功后作废旧 ETag，下一轮必须重新取全量
api.clearResponseCache();
server = (_url, init) => {
  if (init.method === 'POST') return { status: 200, body: { ok: true } };
  if (init.headers['If-None-Match'] === '"v2"') {
    return { status: 304, body: '', headers: { ETag: '"v2"' } };
  }
  return { status: 200, body: { value: 2 }, headers: { ETag: '"v2"' } };
};
await api.getJSON('/etag-post');
await api.postJSON('/etag-post', {});
await api.getJSON('/etag-post');
assert.equal(requests.at(-1).init.headers['If-None-Match'], undefined,
  'POST 之后不能继续用旧校验符');

// ---- POST 清缓存时，正在途中的 304 不能交回旧对象
api.clearResponseCache();
let raceCalls = 0;
let releaseRace;
const raceGate = new Promise((resolve) => { releaseRace = resolve; });
server = async (url) => {
  if (url === '/race-post') return { status: 200, body: { ok: true } };
  raceCalls += 1;
  if (raceCalls === 1) return { status: 200, body: { n: 1 }, headers: { ETag: '"r1"' } };
  if (raceCalls === 2) {
    await raceGate;
    return { status: 304, body: '', headers: { ETag: '"r1"' } };
  }
  return { status: 200, body: { n: 2 }, headers: { ETag: '"r2"' } };
};
await api.getJSON('/race');
const pendingRace = api.getJSON('/race');
await new Promise((resolve) => setTimeout(resolve, 10));
await api.postJSON('/race-post', {});
releaseRace();
assert.deepEqual(await pendingRace, { n: 2 }, '清缓存后的 304 要重取');
assert.equal(requests.filter((x) => x.url === '/race').at(-1).init.headers['If-None-Match'],
  undefined, '重取时不能再带旧校验符');

// ---- 同一 URL 的并发 GET 只发一次请求
api.clearResponseCache();
let dedupeCalls = 0;
server = async () => {
  dedupeCalls += 1;
  await new Promise((resolve) => setTimeout(resolve, 10));
  return { status: 200, body: { shared: true } };
};
const [sharedA, sharedB] = await Promise.all([
  api.getJSON('/dedupe'), api.getJSON('/dedupe'),
]);
assert.equal(dedupeCalls, 1, '并发 GET 要合并成一次');
assert.equal(sharedA, sharedB, '两个调用方拿到同一份结果');

// 非 ASCII 的 Key 放不进请求头，必须忽略，否则 fetch 会在发出前抛错
api.setApiKey('密钥');
assert.equal(api.authHeaders().Authorization, undefined);
api.setApiKey('sk-good');
assert.equal(api.authHeaders().Authorization, 'Bearer sk-good');
api.setApiKey('');

// ---- 查看的版本与网关默认出口相互独立
let exit = 'intl';
server = () => ({ status: 200, body: { current: exit, ok: true } });
globalThis.location = new URL('http://panel.local/');
await store.initRealms();
assert.equal(store.getState().viewRealm, 'intl', '启动时查看的版本跟随默认出口');
exit = 'cn';
globalThis.location = new URL('http://panel.local/?view=intl');
await store.initRealms();
assert.equal(store.getState().viewRealm, 'intl', '地址栏 ?view= 优先');
assert.equal(store.getState().activeRealm, 'cn');
store.setViewRealm('cn');
exit = 'intl';
const seen = [];
const off = store.subscribe((reason) => seen.push(reason));
await store.refreshActiveRealm();
assert.equal(store.getState().activeRealm, 'intl', '默认出口被刷新');
assert.equal(store.getState().viewRealm, 'cn', '刷新默认出口不得改动查看的版本');
assert.deepEqual(seen, ['activeRealm']);
off();
assert.equal(globalThis.location.searchParams.get('view'), 'cn', '手动切换的版本写进地址栏，刷新页面后保持');
assert.throws(() => store.setViewRealm('eu'));

// ---- 格式化边界
assert.equal(format.fmtCredit(0), '0');
assert.equal(format.fmtCredit(0.12345), '0.1235');
assert.equal(format.fmtCredit(12.5), '12.50');
assert.equal(format.fmtCredit(1234.4), '1,234');
assert.equal(format.fmtCompact(999), '999');
assert.equal(format.fmtCompact(1234), '1.2k');
assert.equal(format.fmtCompact(56789), '57k');
assert.equal(format.fmtCompact(3400000), '3.4M');
assert.equal(format.fmtRemain(0), '已过期');
assert.equal(format.fmtRemain(59), '1 分钟');
assert.equal(format.fmtRemain(3600), '1.0 小时');
assert.equal(format.fmtRemain(86400 * 2.5), '2.5 天');
const now = new Date(2026, 9, 1, 0, 30).getTime();
assert.equal(format.fmtDateTimeMarked(new Date(2026, 8, 30, 23, 59, 1).getTime(), now), '昨天 23:59:01', '跨月的前一天也是昨天');
assert.equal(format.fmtDateTimeMarked(new Date(2026, 8, 29, 8, 0, 0).getTime(), now), '09-29 08:00:00');
assert.equal(format.fmtDateTimeMarked(now / 1000, now), '00:30:00', '秒级时间戳同样识别');
assert.equal(format.fmtAgo(now + 5000, now), '刚刚');
assert.equal(format.fmtAgo(null, now), '从未');
assert.equal(format.fmtUntil(now - 1, now), '已到期');
assert.equal(format.fmtUntil(now + 3 * 86400000 + 1000, now), '3 天后');
assert.equal(format.barColor(100), 'var(--success)');
assert.equal(format.barColor(50.1), 'var(--success)');
assert.equal(format.barColor(50), 'var(--warning)', '正好一半按黄色');
assert.equal(format.barColor(25), 'var(--warning)');
assert.equal(format.barColor(24.9), 'var(--danger)');
assert.equal(format.barColor(0), 'var(--danger)');
assert.equal(format.barColor(null), 'var(--danger)');

// ---- 命令面板：名称全等优先，词语需全部命中，空查询保持登记顺序
const entries = palette.paletteEntries();
assert.equal(palette.searchPalette(entries, '').length, entries.length);
assert.ok(entries.some((e) => e.path === 'settings' && e.sub === 'proxy'), '设置页标签可以直接跳转');
assert.equal(palette.searchPalette(entries, '账号')[0].path, 'accounts');
assert.equal(palette.searchPalette(entries, '日志')[0].path, 'requests', '包含「日志」的页面里，登记顺序在前的排前面');
const both = palette.searchPalette(entries, '代理 设置');
assert.equal(both.length, 1);
assert.equal(both[0].sub, 'proxy');
assert.deepEqual(palette.searchPalette(entries, '不存在的页面'), []);

// ---- 路由解析
assert.deepEqual(router.parseHash('#/settings/proxy'), { path: 'settings', sub: 'proxy' });
assert.deepEqual(router.parseHash(''), { path: 'dashboard', sub: '' });
assert.equal(router.hrefFor('settings', 'about'), '#/settings/about');

console.log('web core assertions passed');
