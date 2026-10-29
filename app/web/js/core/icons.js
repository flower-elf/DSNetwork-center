import { unsafeSVG } from './dom.js';

// lucide 图标由 vendor/lucide-*.min.js 以全局变量 lucide 提供；这里拼成 SVG 文本并缓存。
const cache = new Map();

const GITHUB_PATH = 'M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27.68 0 1.36.09 2 .27 1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.012 8.012 0 0 0 16 8c0-4.42-3.58-8-8-8z';

function attrText(attrs) {
  return Object.entries(attrs).map(([k, v]) => `${k}="${String(v).replace(/"/g, '&quot;')}"`).join(' ');
}

function iconSvg(name, cls = '') {
  const key = name + '|' + cls;
  const hit = cache.get(key);
  if (hit) return hit;
  let text;
  if (name === 'Github') {
    text = `<svg class="lucide ${cls}" viewBox="0 0 16 16" fill="currentColor" aria-hidden="true"><path d="${GITHUB_PATH}"/></svg>`;
  } else {
    const node = globalThis.lucide && globalThis.lucide.icons[name];
    if (!node) throw new Error('unknown lucide icon: ' + name);
    const children = node.map(([tag, attrs]) => `<${tag} ${attrText(attrs)}/>`).join('');
    text = `<svg class="lucide ${cls}" xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">${children}</svg>`;
  }
  cache.set(key, text);
  return text;
}

/** lit-html 模板里使用：${icon('Users')}；cls 追加到 svg 的 class 上。 */
export function icon(name, cls = '') {
  return unsafeSVG(iconSvg(name, cls));
}
