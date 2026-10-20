import { html, render, classMap, nothing } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { ROUTES, findRoute } from '../core/routes.js';
import { hrefFor } from '../core/router.js';
import { readStore, writeStore } from '../core/storage.js';

// 底部浮动菜单栏：按分组排列页面入口与快捷动作；鼠标靠近时图标放大。
// 「悬浮」模式可长按空白处拖动位置，「固定」模式始终贴底居中；窄屏收起为右下角按钮。
const POSITION_KEY = 'wb-dock-position';
const MODE_KEY = 'wb-dock-mode';
const MARGIN = 16;
const LONG_PRESS_MS = 180;
const CLICK_SUPPRESS_MS = 220;
const BASE = 40;
const GROW = 30;
const REACH = 150;

export const DOCK_ACTIONS = [
  { id: 'add-account', title: '添加账号', icon: 'CirclePlus' },
  { id: 'panel', title: '面板选项', icon: 'SlidersHorizontal' },
];

let root = null;
let activePath = '';
let onAction = () => {};
let listenersInstalled = false;
let mobileOpen = false;
let suppressClickUntil = 0;
let magnifyFrame = 0;
let magnifyX = 0;

export function dockMode() {
  return readStore(MODE_KEY) === 'pinned' ? 'pinned' : 'floating';
}

export function setDockMode(mode) {
  writeStore(MODE_KEY, mode === 'pinned' ? 'pinned' : null);
  if (mode === 'pinned') writeStore(POSITION_KEY, null);
  applyPosition();
}

function groups() {
  const items = ROUTES.filter((r) => r.dock).map((r) => ({ kind: 'route', ...r }));
  items.push(...DOCK_ACTIONS.map((a) => ({ kind: 'action', group: 'actions', ...a })));
  const out = [];
  for (const item of items) {
    const last = out[out.length - 1];
    if (last && last.key === item.group) last.items.push(item);
    else out.push({ key: item.group, label: item.groupLabel || '', items: [item] });
  }
  return out;
}

function isActive(item) {
  if (item.kind !== 'route') return false;
  const current = findRoute(activePath);
  if (!current) return false;
  if (current.path === item.path) return true;
  if (current.dock || !current.section) return false;
  // 不在菜单栏里的页面，高亮它所属分组的第一个菜单入口。
  const head = ROUTES.find((route) => route.dock && route.section === current.section);
  return Boolean(head) && head.path === item.path;
}

function itemTemplate(item) {
  const label = item.dockTitle || item.title;
  const body = html`${icon(item.icon)}<span class="dock-tip">${label}</span>`;
  const cls = classMap({ 'dock-item': true, active: isActive(item) });
  if (item.kind === 'route') {
    return html`<a class=${cls} href=${hrefFor(item.path)} aria-label=${label} @click=${guardClick}>${body}</a>`;
  }
  return html`<button type="button" class=${cls} aria-label=${label} @click=${(e) => { if (guardClick(e)) return; mobileOpen = false; draw(); onAction(item.id); }}>${body}</button>`;
}

function guardClick(event) {
  if (Date.now() < suppressClickUntil) {
    event.preventDefault();
    return true;
  }
  if (mobileOpen) {
    mobileOpen = false;
    draw();
  }
  return false;
}

function draw() {
  const list = groups();
  render(html`
    <nav class="dock" aria-label="主菜单" @mousemove=${magnify} @mouseleave=${resetMagnify} @pointerdown=${startPress}>
      ${list.map((g, i) => html`
        ${i > 0 ? html`<span class="dock-divider" aria-hidden="true">${g.label ? html`<span class="dock-tip">${g.label}</span>` : nothing}</span>` : nothing}
        ${g.items.map(itemTemplate)}
      `)}
    </nav>
    <button type="button" class="dock-mobile-toggle" aria-label="菜单" aria-expanded=${mobileOpen ? 'true' : 'false'}
            @click=${() => { mobileOpen = !mobileOpen; draw(); }}>${icon('PanelBottomOpen')}</button>
  `, root);
  root.classList.toggle('open', mobileOpen);
}

function applyMagnify(clientX) {
  const items = root.querySelectorAll('.dock .dock-item');
  // 先读完所有几何信息，再统一写样式：读写交替会让每次 mousemove 触发多轮布局。
  const sizes = [];
  for (const el of items) {
    const rect = el.getBoundingClientRect();
    const distance = Math.abs(clientX - (rect.left + rect.width / 2));
    sizes.push((BASE + GROW * Math.max(0, 1 - distance / REACH)).toFixed(1));
  }
  items.forEach((el, index) => {
    el.style.width = sizes[index] + 'px';
    el.style.height = sizes[index] + 'px';
  });
}

function magnify(event) {
  if (window.innerWidth < 768) return;
  magnifyX = event.clientX;
  if (magnifyFrame) return;
  magnifyFrame = requestAnimationFrame(() => {
    magnifyFrame = 0;
    applyMagnify(magnifyX);
  });
}

function resetMagnify() {
  if (magnifyFrame) {
    cancelAnimationFrame(magnifyFrame);
    magnifyFrame = 0;
  }
  for (const el of root.querySelectorAll('.dock .dock-item')) el.style.width = el.style.height = '';
}

function savedPosition() {
  const raw = readStore(POSITION_KEY);
  if (!raw) return null;
  let pos;
  try {
    pos = JSON.parse(raw);
  } catch {
    return null;
  }
  if (!pos || !Number.isFinite(pos.x) || !Number.isFinite(pos.y)
      || !Number.isFinite(pos.vw) || !Number.isFinite(pos.vh)
      || pos.vw <= 0 || pos.vh <= 0) {
    return null;
  }
  // 视口尺寸变化后按比例换算，保持在原来的相对位置
  const x = pos.x * (window.innerWidth / pos.vw);
  const y = pos.y * (window.innerHeight / pos.vh);
  return clamp(x, y);
}

function clamp(x, y) {
  const nav = root.querySelector('.dock');
  const width = nav ? nav.offsetWidth : 0;
  const height = nav ? nav.offsetHeight : 64;
  const half = width / 2;
  return {
    x: Math.min(Math.max(x, half + MARGIN), window.innerWidth - half - MARGIN),
    y: Math.min(Math.max(y, MARGIN), window.innerHeight - height - MARGIN),
  };
}

function applyPosition(pos) {
  const p = pos || (dockMode() === 'floating' ? savedPosition() : null);
  root.classList.toggle('floating', !!p);
  root.style.left = p ? p.x + 'px' : '';
  root.style.top = p ? p.y + 'px' : '';
}

function startPress(event) {
  if (dockMode() !== 'floating' || window.innerWidth < 768 || event.button !== 0) return;
  if (event.target.closest('a,button,input,select,textarea')) return;
  const startX = event.clientX;
  const startY = event.clientY;
  const rect = root.getBoundingClientRect();
  const offsetX = startX - (rect.left + rect.width / 2);
  const offsetY = startY - rect.top;
  let dragging = false;
  const timer = setTimeout(() => { dragging = true; root.style.cursor = 'grabbing'; }, LONG_PRESS_MS);
  let last = null;
  const move = (e) => {
    if (!dragging) {
      if (Math.hypot(e.clientX - startX, e.clientY - startY) > 4) cleanup();
      return;
    }
    last = clamp(e.clientX - offsetX, e.clientY - offsetY);
    applyPosition(last);
  };
  const up = () => {
    if (dragging && last) {
      writeStore(POSITION_KEY, JSON.stringify({ x: last.x, y: last.y, vw: window.innerWidth, vh: window.innerHeight }));
      suppressClickUntil = Date.now() + CLICK_SUPPRESS_MS;
    }
    cleanup();
  };
  function cleanup() {
    clearTimeout(timer);
    root.style.cursor = '';
    window.removeEventListener('pointermove', move);
    window.removeEventListener('pointerup', up);
  }
  window.addEventListener('pointermove', move);
  window.addEventListener('pointerup', up);
}

export function mountDock(element, actionHandler) {
  root = element;
  root.className = 'dock-root';
  onAction = actionHandler;
  draw();
  applyPosition();
  if (listenersInstalled) return;
  listenersInstalled = true;
  window.addEventListener('resize', () => applyPosition());
  document.addEventListener('click', (e) => {
    if (mobileOpen && !root.contains(e.target)) {
      mobileOpen = false;
      draw();
    }
  });
}

export function setDockActive(path) {
  activePath = path;
  if (root) draw();
}
