import { html, render, nothing, live } from './core/dom.js';
import { icon } from './core/icons.js';
import { getJSON, postJSON, onUnauthorized, setApiKey, setPanelToken } from './core/api.js';
import { getState, subscribe, initRealms, setViewRealm, setPanelStatus, setVersion } from './core/store.js';
import { startRouter, stopRouter, remount, navigate, currentRoute } from './core/router.js';
import { refreshSeconds, setRefreshSeconds, onRefreshSeconds } from './core/refresh.js';
import { initTheme, themeMode, setThemeMode } from './core/theme.js';
import { mountDock, setDockActive, dockMode, setDockMode } from './ui/dock.js';
import { installPaletteShortcut, paletteTrigger } from './ui/palette.js';
import { openDialog } from './ui/dialog.js';
import { updateAvailable } from './core/update.js';
import { openUpdateDialog } from './ui/update.js';
import { toast, toastError } from './ui/toast.js';
import { showLogin } from './pages/login.js';

const app = document.getElementById('app');
// 顶栏状态点来自 /panel/status，该接口只在带面板会话时返回。
let gateway = { ok: null, ready: 0, total: 0 };
let gatewayTimer = null;
let shellRunning = false;

// 地址栏里的 ?key=（API Key）与 ?pwd=（面板密码）读取后立即移除，其余参数保留。
function takeUrlParam(name) {
  const params = new URLSearchParams(location.search);
  const value = params.get(name);
  if (value === null) return null;
  params.delete(name);
  const qs = params.toString();
  history.replaceState(null, '', location.pathname + (qs ? '?' + qs : '') + location.hash);
  return value;
}

function topbar() {
  const { viewRealm, version } = getState();
  const dotClass = gateway.ok === null ? '' : gateway.ok ? 'ok' : 'bad';
  const dotTitle = gateway.ok === null ? '正在检查网关状态' : gateway.ok ? `网关运行中，${gateway.ready} / ${gateway.total} 个账号可用` : '网关没有可用账号或无法连接';
  return html`
    <div class="topbar">
      <a class="topbar-brand" href="#/dashboard" style="text-decoration:none">
        <span class="brand-mark">${icon('Bot')}</span>
        <span>WorkBuddy 网关</span>
        <span class="status-dot ${dotClass}" title=${dotTitle}></span>
        ${version ? html`<span class="text-2xs muted medium">v${version}</span>` : nothing}
      </a>
      <div class="topbar-tools">
        ${paletteTrigger()}
        <div class="realm-toggle" role="radiogroup" aria-label="查看的版本">
          <button type="button" class=${viewRealm === 'intl' ? 'active' : ''} role="radio" aria-checked=${viewRealm === 'intl' ? 'true' : 'false'}
                  title="查看国际版 (www.workbuddy.ai) 的账号与模型" @click=${() => setViewRealm('intl')}>${icon('Globe')}国际版</button>
          <button type="button" class=${viewRealm === 'cn' ? 'active' : ''} role="radio" aria-checked=${viewRealm === 'cn' ? 'true' : 'false'}
                  title="查看国内版 (www.workbuddy.cn) 的账号与模型" @click=${() => setViewRealm('cn')}>${icon('House')}国内版</button>
        </div>
      </div>
    </div>`;
}

function drawTopbar() {
  const el = document.getElementById('topbar');
  if (el) render(topbar(), el);
}

async function pollGateway() {
  try {
    const data = await getJSON('/panel/status');
    gateway = { ok: !!data.accounts_ready, ready: data.accounts_ready || 0, total: data.accounts || 0 };
  } catch {
    gateway = { ok: false, ready: 0, total: 0 };
  }
  drawTopbar();
}

// 每次打开面板最多提示一次新版本；登录成功与恢复会话都走这里。
let updatePrompted = false;
const UPDATE_POLL_MS = 500;
const UPDATE_POLL_TRIES = 20;

async function promptUpdateIfAny() {
  if (updatePrompted) return;
  // 启动检查可能在登录时还没跑完（checking=true）：有界等它，别就此放弃。
  for (let attempt = 0; attempt < UPDATE_POLL_TRIES; attempt += 1) {
    let status;
    try {
      status = await getJSON('/updates');
    } catch {
      return;
    }
    if (status && status.checking && attempt + 1 < UPDATE_POLL_TRIES) {
      await new Promise((resolve) => setTimeout(resolve, UPDATE_POLL_MS));
      continue;
    }
    // 自动检查关闭时不打扰：旧结果可能是关闭前留下的。
    if (!status || !status.enabled || !updateAvailable(status)) return;
    updatePrompted = true;
    openUpdateDialog(status);
    return;
  }
}

function openPanelOptions() {
  const status = getState().panelStatus;
  const ctl = openDialog({
    width: 520,
    title: '面板选项',
    icon: 'SlidersHorizontal',
    desc: '外观、菜单栏与自动刷新只影响本浏览器的显示方式，修改后立即生效。',
    body: () => html`
      ${status.panel_password_is_default ? html`
        <div class="notice notice-warn">${icon('TriangleAlert')}
          <div class="grow">面板仍在使用默认密码 admin，局域网内的其他人也能打开本页面。</div>
          <button class="btn btn-ghost" @click=${() => { ctl.close(); navigate('settings', 'security'); }}>去修改</button>
        </div>` : nothing}
      <div class="col">
        <span class="section-label">主题</span>
        <div class="pill-group">
          ${[['system', '跟随系统', 'Monitor'], ['light', '浅色', 'Sun'], ['dark', '深色', 'Moon']].map(([mode, label, ic]) => html`
            <button type="button" class="pill-btn ${themeMode() === mode ? 'active' : ''}" @click=${() => { setThemeMode(mode); ctl.update(); }}>${icon(ic)}${label}</button>`)}
        </div>
      </div>
      <div class="col">
        <span class="section-label">菜单栏位置</span>
        <div class="pill-group">
          <button type="button" class="pill-btn ${dockMode() === 'floating' ? 'active' : ''}" @click=${() => { setDockMode('floating'); ctl.update(); }}>${icon('Move')}悬浮，长按空白处拖动</button>
          <button type="button" class="pill-btn ${dockMode() === 'pinned' ? 'active' : ''}" @click=${() => { setDockMode('pinned'); ctl.update(); }}>${icon('PanelBottom')}固定在底部</button>
        </div>
      </div>
      <div class="col">
        <span class="section-label">自动刷新间隔</span>
        <div class="row">
          <input class="input input-num" type="number" min="0" max="3600" step="1" inputmode="numeric"
                 aria-label="自动刷新间隔（秒）"
                 .value=${live(String(refreshSeconds()))}
                 @change=${(e) => { setRefreshSeconds(e.target.value); ctl.update(); }}>
          <span class="muted text-xs">秒</span>
        </div>
        <span class="field-hint">填写 0 表示不自动刷新。</span>
      </div>
      <div class="col">
        <span class="section-label">链接</span>
        <div class="pill-group">
          <a class="pill-btn" href="https://github.com/flower-elf/DSNetwork-center" target="_blank" rel="noopener noreferrer">${icon('Github')}GitHub 项目仓库</a>
          <button type="button" class="pill-btn" @click=${() => { ctl.close(); navigate('keys'); }}>${icon('KeyRound')}客户端接入</button>
        </div>
      </div>
      <hr class="divider">
      <div class="row-between text-xs">
        <span class="muted">WorkBuddy 网关 ${getState().version ? 'v' + getState().version : ''}</span>
        <button class="btn btn-ghost btn-sm tone-danger" @click=${logout}>${icon('LogOut')}退出登录</button>
      </div>`,
  });
}

async function logout() {
  await postJSON('/panel/logout', {});
  setPanelToken('');
  location.reload();
}

async function onDockAction(id) {
  if (id === 'panel') openPanelOptions();
  if (id === 'add-account') {
    const dialogs = await import('./pages/accounts/dialogs/add.js');
    dialogs.openAddAccountDialog();
  }
}

async function startShell() {
  if (shellRunning) return;
  shellRunning = true;
  try {
    const [status, settings] = await Promise.all([getJSON('/panel/status'), getJSON('/settings')]);
    setPanelStatus(status);
    setVersion(settings.version);
    await initRealms();
    render(html`
      <div class="shell">
        <div class="shell-body">
          <div id="topbar"></div>
          <main id="pageHost"></main>
        </div>
        <div id="dock"></div>
      </div>`, app);
    drawTopbar();
    mountDock(document.getElementById('dock'), onDockAction);
    await startRouter(document.getElementById('pageHost'), (route) => setDockActive(route.path));
    setDockActive(currentRoute().path);
    pollGateway();
    armGatewayTimer();
    promptUpdateIfAny();
  } catch (err) {
    // 失败必须放开这把锁：否则登录成功后 startShell 直接返回，面板永远停在
    // 禁用的登录框上。
    shellRunning = false;
    throw err;
  }
}

/** 按设置的间隔轮询顶栏状态点；间隔为 0 时不挂定时器。 */
function armGatewayTimer() {
  clearInterval(gatewayTimer);
  gatewayTimer = null;
  const seconds = refreshSeconds();
  if (!shellRunning || seconds <= 0) return;
  gatewayTimer = setInterval(pollGateway, seconds * 1000);
}

function stopShell() {
  shellRunning = false;
  stopRouter();
  clearInterval(gatewayTimer);
  gatewayTimer = null;
}

function showLoginScreen(message) {
  showLogin(app, {
    message,
    autoPassword: takeUrlParam('pwd') || '',
    onSuccess: async (data) => {
      if (data.using_default_password) toast.warn('当前是默认密码 admin', '建议到「设置 → 面板密码」修改');
      try {
        await startShell();
      } catch (err) {
        toastError('加载面板失败', err);
      }
    },
  });
}

async function boot() {
  initTheme();
  installPaletteShortcut();
  onRefreshSeconds(armGatewayTimer);
  const key = takeUrlParam('key');
  if (key) setApiKey(key);
  subscribe((reason) => {
    if (reason === 'viewRealm') { drawTopbar(); remount(); }
    if (reason === 'version' || reason === 'activeRealm') drawTopbar();
  });
  onUnauthorized(() => {
    if (!shellRunning) return;
    stopShell();
    setPanelToken('');
    showLoginScreen('会话已失效，请重新输入密码');
  });
  try {
    setPanelStatus(await getJSON('/panel/status'));
    await startShell();
  } catch {
    // 没有面板会话时这个接口回 401，此时只显示登录页。
    showLoginScreen('');
  }
}

boot();
