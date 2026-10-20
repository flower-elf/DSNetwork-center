import { readStore, writeStore } from './storage.js';

// 面板会话令牌放 sessionStorage，关闭标签页即失效；API Key 可经 ?key= 传入后记在 localStorage。
const PANEL_STORE = 'wb-proxy-center-panel-token';
const KEY_STORE = 'wb-proxy-center-api-key';

let panelToken = readStore(PANEL_STORE, 'session') || '';
let apiKey = (readStore(KEY_STORE) || '').trim();
let unauthorizedHandler = null;

// 轮询接口的 GET 响应按 URL 记住 ETag 与已解析结果：后端命中 If-None-Match 时
// 回 304，前端直接复用上一次的对象，不再下载和解析整份 JSON。任何写请求成功
// 后整体作废，避免拿变更前的缓存继续展示。
const etagCache = new Map();
// 同一 URL 的并发 GET 共用一个 Promise：首屏多个页面同时取 /settings 时只发一次。
const inflight = new Map();
// 清空缓存时递增；清空之前就发出的请求不许把旧结果写回来。
let cacheEpoch = 0;
// 轮询用的轻量 GET 与可能冷扫描大日志的用量接口分开限时；POST 不设统一截止时间，
// 批量积分刷新、批量任务这类合法长操作由服务端自己决定何时结束。
const LIGHT_GET_TIMEOUT_MS = 30000;
const HEAVY_GET_TIMEOUT_MS = 180000;
const LOGIN_TIMEOUT_MS = 30000;
const MAX_CACHED_GETS = 32;

function getTimeout(url) {
  return /^\/usage(\/|\?|$)/.test(url) ? HEAVY_GET_TIMEOUT_MS : LIGHT_GET_TIMEOUT_MS;
}

function rememberResponse(url, etag, data) {
  if (etagCache.size >= MAX_CACHED_GETS) {
    const oldest = etagCache.keys().next().value;
    etagCache.delete(oldest);
  }
  etagCache.set(url, { etag, data });
}

export class ApiError extends Error {
  constructor(status, message, body) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
  }
}

export function isAuthError(err) {
  return err instanceof ApiError && err.status === 401;
}

/** 清空条件缓存；登录状态变化或写请求成功后调用。 */
export function clearResponseCache() {
  cacheEpoch += 1;
  etagCache.clear();
  inflight.clear();
}

export function setPanelToken(token) {
  panelToken = token || '';
  writeStore(PANEL_STORE, panelToken || null, 'session');
  clearResponseCache();
}

export function setApiKey(key) {
  apiKey = (key || '').trim();
  writeStore(KEY_STORE, apiKey || null);
  clearResponseCache();
}

/** 会话失效时的统一处理，由 main.js 注册。 */
export function onUnauthorized(handler) {
  unauthorizedHandler = handler;
}

// 含非 ASCII 字符的值放不进请求头，fetch 会直接抛错；这样的值不可能是网关 Key，忽略它。
function usableKey(value) {
  return value && /^[\x20-\x7e]+$/.test(value) ? value : '';
}

export function authHeaders(base = {}) {
  const headers = { ...base };
  const key = usableKey(apiKey);
  if (key) headers.Authorization = 'Bearer ' + key;
  if (panelToken) headers['X-Panel-Token'] = panelToken;
  return headers;
}

function errorMessage(status, parsed) {
  if (parsed && typeof parsed === 'object') {
    if (parsed.error && parsed.error.message) return parsed.error.message;
    if (parsed.msg) return parsed.msg;
    if (parsed.raw) return String(parsed.raw).slice(0, 200);
  }
  return 'HTTP ' + status;
}

async function fetchWithTimeout(url, init, timeoutMs) {
  if (!timeoutMs || timeoutMs <= 0) return fetch(url, init);
  const controller = new AbortController();
  let timedOut = false;
  const timer = setTimeout(() => {
    timedOut = true;
    controller.abort();
  }, timeoutMs);
  try {
    return await fetch(url, { ...init, signal: controller.signal });
  } catch (err) {
    if (timedOut && err && err.name === 'AbortError') {
      throw new ApiError(0, '请求超时，请检查网关是否在线');
    }
    throw err;
  } finally {
    clearTimeout(timer);
  }
}

async function requestOnce(method, url, body, retried = false) {
  const epoch = cacheEpoch;
  const cached = method === 'GET' ? etagCache.get(url) : null;
  const headers = authHeaders(body === undefined ? {} : { 'Content-Type': 'application/json' });
  if (cached) headers['If-None-Match'] = cached.etag;
  const init = { method, cache: 'no-store', headers };
  if (body !== undefined) init.body = JSON.stringify(body);
  const response = await fetchWithTimeout(
    url, init, method === 'GET' ? getTimeout(url) : 0);
  // 304 没有响应体，直接交回上一次解析好的对象；但缓存若已被 POST 清空，
  // 这个 304 描述的是清空前的版本，必须去掉校验符重取一次。
  if (method === 'GET' && cached && response.status === 304) {
    if (epoch === cacheEpoch) return cached.data;
    if (!retried) return requestOnce(method, url, body, true);
  }
  const text = await response.text();
  let parsed = {};
  if (text) {
    try {
      parsed = JSON.parse(text);
    } catch {
      parsed = { raw: text };
    }
  }
  if (method === 'GET' && response.headers && response.headers.get) {
    const etag = response.headers.get('ETag');
    if (etag && response.ok && epoch === cacheEpoch) {
      rememberResponse(url, etag, parsed);
    }
  }
  if (response.status === 401) {
    // 修改密码接口用 401 表示「当前密码不对」，那是表单错误，不能当成会话
    // 失效把整个面板登出。
    if (url.startsWith('/panel/password')) {
      throw new ApiError(401, errorMessage(401, parsed), parsed);
    }
    if (unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(401, '面板会话已失效，请重新登录', parsed);
  }
  if (response.status === 403) {
    // 权限不足不等于会话失效：例如只用 API Key 的会话修改默认出口。
    throw new ApiError(403, errorMessage(403, parsed), parsed);
  }
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, parsed), parsed);
  return parsed;
}

async function request(method, url, body) {
  if (method !== 'GET') {
    const data = await requestOnce(method, url, body);
    // 写请求可能改变任何列表：让下一轮读取重新协商，不能继续用旧 ETag。
    clearResponseCache();
    return data;
  }
  const pending = inflight.get(url);
  if (pending) return pending;
  const run = requestOnce(method, url, body);
  inflight.set(url, run);
  const settle = () => { if (inflight.get(url) === run) inflight.delete(url); };
  run.then(settle, settle);
  return run;
}

export function getJSON(url) {
  return request('GET', url);
}

export function postJSON(url, body = {}) {
  return request('POST', url, body);
}

/** 以面板会话下载文件：普通链接带不上 X-Panel-Token 请求头，所以先取回再交给浏览器保存。 */
export async function downloadFile(url, fallbackName) {
  const response = await fetch(url, { cache: 'no-store', headers: authHeaders() });
  if (response.status === 401 || response.status === 403) {
    if (unauthorizedHandler) unauthorizedHandler();
    throw new ApiError(response.status, '面板会话已失效，请重新登录', null);
  }
  if (!response.ok) throw new ApiError(response.status, 'HTTP ' + response.status, null);
  const disposition = response.headers.get('Content-Disposition') || '';
  const match = disposition.match(/filename="?([^";]+)"?/i);
  const name = match ? match[1] : fallbackName;
  const blob = await response.blob();
  const href = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = href;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(href), 1000);
  return name;
}

/** 登录接口不带会话令牌，单独处理。 */
export async function panelLogin(password) {
  const response = await fetchWithTimeout('/panel/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ password }),
  }, LOGIN_TIMEOUT_MS);
  const data = await response.json();
  if (!response.ok) throw new ApiError(response.status, errorMessage(response.status, data), data);
  setPanelToken(data.token || '');
  return data;
}
