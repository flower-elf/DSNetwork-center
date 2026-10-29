import { html, classMap } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { navigate } from '../core/router.js';
import { openDialog } from './dialog.js';
import { paletteEntries, searchPalette } from './palette-logic.js';

// 命令面板：Ctrl+K / ⌘K 打开；只做跳转，不执行任何操作。
const isMac = /Mac|iPhone|iPad/.test(navigator.platform || navigator.userAgent);
const PALETTE_SHORTCUT = isMac ? '⌘K' : 'Ctrl K';

let ctl = null;

function openPalette() {
  if (ctl) return;
  const entries = paletteEntries();
  let query = '';
  let results = entries;
  let active = 0;

  const go = (entry) => {
    ctl.close();
    navigate(entry.path, entry.sub);
  };

  const onKey = (event) => {
    if (event.isComposing) return;
    if (event.key === 'ArrowDown') { active = (active + 1) % Math.max(results.length, 1); }
    else if (event.key === 'ArrowUp') { active = (active - 1 + results.length) % Math.max(results.length, 1); }
    else if (event.key === 'Home') { active = 0; }
    else if (event.key === 'End') { active = Math.max(results.length - 1, 0); }
    else if (event.key === 'Enter') { if (results[active]) go(results[active]); return; }
    else return;
    event.preventDefault();
    ctl.update();
    const el = ctl.element.querySelector('.palette-item.active');
    if (el) el.scrollIntoView({ block: 'nearest' });
  };

  ctl = openDialog({
    top: true,
    width: 560,
    className: 'palette',
    onClose: () => { ctl = null; },
    raw: () => html`
      <div class="palette-search">
        ${icon('Search')}
        <input autofocus placeholder="输入页面名称，例如「账号」「代理槽」" .value=${query} @keydown=${onKey}
               @input=${(e) => { query = e.target.value; results = searchPalette(entries, query); active = 0; ctl.update(); }}>
      </div>
      <div class="palette-list scroll-slim" role="listbox">
        ${results.length ? results.map((entry, i) => html`
          <button type="button" role="option" class=${classMap({ 'palette-item': true, active: i === active })}
                  aria-selected=${i === active ? 'true' : 'false'}
                  @mousemove=${() => { if (active !== i) { active = i; ctl.update(); } }} @click=${() => go(entry)}>
            ${icon(entry.icon)}<span>${entry.label}</span><span class="palette-group">${entry.group}</span>
          </button>`) : html`<div class="palette-empty">没有匹配的页面<br>试试「账号」「密钥」「设置」</div>`}
      </div>
      <div class="palette-foot">
        <span>↑↓ 选择 · Enter 打开 · Esc 关闭</span>
        <span>共 ${results.length} 项</span>
      </div>`,
  });
}

export function installPaletteShortcut() {
  document.addEventListener('keydown', (event) => {
    if ((event.metaKey || event.ctrlKey) && !event.altKey && event.key.toLowerCase() === 'k') {
      event.preventDefault();
      if (ctl) ctl.close();
      else openPalette();
    }
  });
}

export function paletteTrigger() {
  return html`
    <button type="button" class="pill-trigger" @click=${openPalette} title="搜索页面">
      ${icon('Search')}<span>搜索</span><kbd>${PALETTE_SHORTCUT}</kbd>
    </button>`;
}
