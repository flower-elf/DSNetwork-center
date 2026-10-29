import { readStore, writeStore } from './storage.js';

// 主题：light / dark / system；首屏由 js/theme-boot.js 提前设置，这里负责切换与跟随系统。
const KEY = 'wb-theme';
const media = globalThis.matchMedia ? globalThis.matchMedia('(prefers-color-scheme: dark)') : null;

export function themeMode() {
  const saved = readStore(KEY);
  return saved === 'light' || saved === 'dark' ? saved : 'system';
}

function isDark() {
  return document.documentElement.classList.contains('dark');
}

function apply() {
  const mode = themeMode();
  const dark = mode === 'dark' || (mode === 'system' && !!media && media.matches);
  if (dark === isDark()) return;
  document.documentElement.classList.toggle('dark', dark);
  document.dispatchEvent(new CustomEvent('wb-theme-change'));
}

export function setThemeMode(mode) {
  if (!['light', 'dark', 'system'].includes(mode)) throw new Error('unknown theme mode: ' + mode);
  writeStore(KEY, mode === 'system' ? null : mode);
  apply();
}

export function initTheme() {
  apply();
  if (media) media.addEventListener('change', apply);
}
