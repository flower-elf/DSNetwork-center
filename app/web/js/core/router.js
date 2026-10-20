import { ROUTES, findRoute } from './routes.js';
import { refreshSeconds, onRefreshSeconds } from './refresh.js';

// 路由：地址形如 #/accounts 或 #/settings/proxy。
// 页面模块导出 mount(host, ctx)，返回 { refresh, unmount }；另有 autoRefresh 表示按设置的间隔刷新，
// refreshMs 表示按页面自带的固定节奏刷新。
let host = null;
let current = null;      // { route, sub, instance, timer }
let generation = 0;
let onChange = () => {};
let navGuard = null;

/** 页面在挂载期间可以注册离开前的询问：返回 false 表示取消这次跳转。 */
export function setNavigationGuard(fn) {
  navGuard = fn;
}

export function parseHash(hash = location.hash) {
  const parts = hash.replace(/^#\/?/, '').split('/').filter(Boolean);
  return { path: parts[0] || ROUTES[0].path, sub: parts[1] || '' };
}

export function hrefFor(path, sub = '') {
  return '#/' + path + (sub ? '/' + sub : '');
}

export function navigate(path, sub = '') {
  const target = hrefFor(path, sub);
  if (location.hash === target) return;
  location.hash = target;
}

export function currentRoute() {
  return current ? { path: current.route.path, sub: current.sub } : parseHash();
}

function stopCurrent() {
  if (!current) return;
  clearInterval(current.timer);
  if (current.instance && current.instance.unmount) current.instance.unmount();
  current = null;
}

async function runRefresh(entry) {
  if (!entry.instance || !entry.instance.refresh || entry.busy || document.hidden) return;
  entry.busy = true;
  try {
    await entry.instance.refresh();
  } finally {
    entry.busy = false;
  }
}

/** 页面自带固定节奏时用 refreshMs，否则用设置的间隔；间隔为 0 或页面不自动刷新就不挂定时器。 */
function armRefresh(entry) {
  if (entry.timer) clearInterval(entry.timer);
  entry.timer = null;
  const seconds = refreshSeconds();
  const ms = entry.fixedMs > 0 ? entry.fixedMs : (entry.auto && seconds > 0 ? seconds * 1000 : 0);
  if (ms > 0) entry.timer = setInterval(() => runRefresh(entry), ms);
}

onRefreshSeconds(() => { if (current) armRefresh(current); });

function showLoadError(box, err) {
  box.replaceChildren();
  const page = document.createElement('div');
  page.className = 'page';
  const alert = document.createElement('div');
  alert.className = 'load-error';
  alert.setAttribute('role', 'alert');
  const mark = document.createElement('div');
  mark.className = 'load-error-icon';
  mark.textContent = '!';
  const title = document.createElement('div');
  title.className = 'text-sm medium';
  title.textContent = '页面加载失败：' + (err && err.message ? err.message : String(err));
  const hint = document.createElement('div');
  hint.className = 'text-xs muted';
  hint.textContent = '可以先重试；如果反复出现，请查看运行日志页里的报错。';
  const retry = document.createElement('button');
  retry.type = 'button';
  retry.className = 'btn btn-outline btn-sm';
  retry.textContent = '重试';
  retry.addEventListener('click', () => show());
  alert.append(mark, title, hint, retry);
  page.appendChild(alert);
  box.appendChild(page);
}

async function show() {
  const { path, sub } = parseHash();
  const route = findRoute(path);
  if (!route) {
    navigate(ROUTES[0].path);
    return;
  }
  if (navGuard && current && (path !== current.route.path || sub !== current.sub)) {
    const ok = await navGuard({ path, sub });
    if (!ok) {
      // 取消跳转：地址栏改回当前页面，不重新挂载。
      history.replaceState(null, '', hrefFor(current.route.path, current.sub));
      return;
    }
  }
  const mine = ++generation;
  stopCurrent();
  const entry = { route, sub, instance: null, timer: null, busy: false, auto: false, fixedMs: 0 };
  current = entry;
  let mod;
  try {
    mod = await route.load();
  } catch (err) {
    if (mine !== generation) return;
    host.replaceChildren();
    const box = document.createElement('div');
    host.appendChild(box);
    showLoadError(box, err);
    document.title = route.title + ' - WorkBuddy 网关';
    onChange(route, sub);
    return;
  }
  if (mine !== generation) return;
  host.replaceChildren();
  const mount = document.createElement('div');
  host.appendChild(mount);
  entry.auto = !!mod.autoRefresh;
  entry.fixedMs = mod.refreshMs || 0;
  try {
    entry.instance = mod.mount(mount, { sub, route });
  } catch (err) {
    showLoadError(mount, err);
    document.title = route.title + ' - WorkBuddy 网关';
    onChange(route, sub);
    return;
  }
  armRefresh(entry);
  document.title = route.title + ' - WorkBuddy 网关';
  onChange(route, sub);
}

/** 切换查看的版本后调用，重新挂载当前页面，旧版本的数据不会残留在新版本里。 */
export function remount() {
  if (current) show();
}

export function startRouter(hostElement, changed) {
  host = hostElement;
  onChange = changed || (() => {});
  window.addEventListener('hashchange', show);
  document.addEventListener('visibilitychange', onVisible);
  return show();
}

export function stopRouter() {
  window.removeEventListener('hashchange', show);
  document.removeEventListener('visibilitychange', onVisible);
  generation++;
  stopCurrent();
}

function onVisible() {
  if (!document.hidden && current) runRefresh(current);
}
