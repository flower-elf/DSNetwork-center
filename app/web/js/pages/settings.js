// 设置页：标签由地址栏决定（#/settings/<id>），各自是一个独立模块。
import { html, render, nothing } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON, postJSON } from '../core/api.js';
import { SETTINGS_TABS } from '../core/routes.js';
import { hrefFor, setNavigationGuard } from '../core/router.js';
import { loadErrorPage, sectionTabs, skeletonRows } from '../ui/widgets.js';
import { openDialog } from '../ui/dialog.js';
import { toastError } from '../ui/toast.js';
import { createAboutTab } from './settings/about.js';
import { createAdvancedTab } from './settings/advanced.js';
import { createCostTab } from './settings/estimate.js';
import { createGatewayTab } from './settings/gateway.js';
import { createMaintenanceTab } from './settings/maintenance.js';
import { createProxyTab } from './settings/proxy.js';
import { createSecurityTab } from './settings/security.js';

// 设置项随标签切换重新读取，不做定时刷新。

export function mount(host, ctx) {
  const activeId = SETTINGS_TABS.some((tab) => tab.id === ctx.sub) ? ctx.sub : SETTINGS_TABS[0].id;
  const state = { view: null, loading: true, loaded: false, error: '' };

  function draw() {
    render(view(), host);
  }

  /** 保存设置，成功时返回服务端回写的整份设置视图。 */
  async function save(payload) {
    const data = await postJSON('/settings/save', payload);
    state.view = data;
    return data;
  }

  async function loadSettings() {
    if (!state.loaded) {
      state.loading = true;
      draw();
    }
    try {
      const data = await getJSON('/settings');
      state.view = data;
      state.error = '';
      state.loading = false;
      state.loaded = true;
      tabs.gateway.seed(data);
      tabs.cost.seed(data);
      tabs.advanced.seed(data);
      tabs.maintenance.seed(data);
      draw();
      return true;
    } catch (err) {
      state.loading = false;
      state.error = err.message;
      draw();
      return false;
    }
  }

  /** 重新读取设置，失败只给出提示：调用方多半刚完成一个写操作。 */
  async function reloadSettings() {
    try {
      const data = await getJSON('/settings');
      state.view = data;
      state.error = '';
      tabs.gateway.seed(data);
      tabs.cost.seed(data);
      tabs.advanced.seed(data);
      tabs.maintenance.seed(data);
    } catch (err) {
      toastError('设置刷新失败', err);
    }
    draw();
  }

  /** 未保存的改动：保存 / 放弃 / 取消。返回 'save' | 'discard' | 'cancel'。 */
  function confirmUnsaved({ desc, onSave }) {
    return new Promise((resolve) => {
      let busy = false;
      let settled = false;
      const done = (choice) => {
        if (settled) return;
        settled = true;
        resolve(choice);
      };
      const ctl = openDialog({
        width: 440,
        icon: 'TriangleAlert',
        title: '有未保存的改动',
        desc,
        onClose: () => done('cancel'),
        foot: () => html`
          <button class="btn btn-ghost" ?disabled=${busy} @click=${() => ctl.close()}>取消</button>
          <button class="btn btn-outline" ?disabled=${busy} @click=${() => { done('discard'); ctl.close(); }}>放弃</button>
          <button class="btn btn-primary" ?disabled=${busy} @click=${save}>
            ${busy ? icon('LoaderCircle', 'spin') : nothing}保存
          </button>`,
      });
      async function save() {
        busy = true;
        ctl.dismissible = false;
        ctl.update();
        const saved = await onSave();
        done(saved ? 'save' : 'cancel');
        ctl.close();
      }
    });
  }

  const page = { state, draw, save, reloadSettings, confirmUnsaved };

  const tabs = {
    gateway: createGatewayTab(page),
    cost: createCostTab(page),
    advanced: createAdvancedTab(page),
    maintenance: createMaintenanceTab(page),
    proxy: createProxyTab(page),
    security: createSecurityTab(page),
    about: createAboutTab(page),
  };
  const active = tabs[activeId];

  // 离开这个标签或整个设置页之前，先处理没保存的改动。
  let leaving = false;
  setNavigationGuard(async () => {
    if (leaving) return false;
    if (!active.canLeave) return true;
    leaving = true;
    try {
      return await active.canLeave();
    } finally {
      leaving = false;
    }
  });

  function header() {
    const navItems = SETTINGS_TABS.map((tab) => ({
      id: tab.id,
      label: tab.title,
      icon: tab.icon,
      href: hrefFor('settings', tab.id),
    }));
    return html`
      <div class="page-head">
        <div class="col" style="gap:10px">
          <div>
            <h1 class="page-title">设置</h1>
            <p class="page-desc">网关行为、费用估算、代理槽、面板密码、高级功能、运维与运行信息</p>
          </div>
          ${sectionTabs(navItems, activeId)}
        </div>
      </div>`;
  }

  function view() {
    if (state.loading) {
      return html`<div class="page">${header()}${skeletonRows(4)}</div>`;
    }
    if (!state.loaded) {
      return html`<div class="page">${header()}${loadErrorPage('读取设置失败：' + (state.error || '未知原因'), () => loadSettings())}</div>`;
    }
    return html`
      <div class="page">
        ${header()}
        ${active.render()}
      </div>`;
  }

  loadSettings().then(() => {
    if (active.onMount) active.onMount();
  });
  draw();

  return {
    unmount() {
      setNavigationGuard(null);
    },
  };
}
