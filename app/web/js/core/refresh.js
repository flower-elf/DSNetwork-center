// 自动刷新间隔：保存在 localStorage，单位秒，默认 60，0 表示不自动刷新；页面导出 autoRefresh 时按它刷新。
import { readStore, writeStore } from './storage.js';

const DEFAULT_REFRESH_SECONDS = 60;
const KEY = 'wb-refresh-interval';
const MAX_SECONDS = 3600;

const listeners = new Set();

export function refreshSeconds() {
  const value = Number.parseInt(readStore(KEY) ?? '', 10);
  if (!Number.isFinite(value) || value < 0) return DEFAULT_REFRESH_SECONDS;
  return Math.min(value, MAX_SECONDS);
}

/** 返回规整后的秒数，空值或非法值回到默认。 */
export function setRefreshSeconds(value) {
  const parsed = Number.parseInt(value, 10);
  const seconds = Number.isFinite(parsed)
    ? Math.min(Math.max(parsed, 0), MAX_SECONDS)
    : DEFAULT_REFRESH_SECONDS;
  writeStore(KEY, seconds === DEFAULT_REFRESH_SECONDS ? null : seconds);
  for (const fn of listeners) fn(seconds);
  return seconds;
}

export function onRefreshSeconds(fn) {
  listeners.add(fn);
  return () => listeners.delete(fn);
}
