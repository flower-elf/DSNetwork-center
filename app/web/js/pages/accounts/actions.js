// 账号页用到的后端接口，页面与弹窗共用同一套调用与命名。

import { downloadFile, getJSON, postJSON } from '../../core/api.js';

/** 账号数据变化后由弹窗广播，账号页收到就重新读取。 */
export const ACCOUNTS_CHANGED = 'wb-accounts-changed';

export function notifyAccountsChanged() {
  window.dispatchEvent(new CustomEvent(ACCOUNTS_CHANGED));
}

export function loadAccounts() {
  return getJSON('/accounts?realm=all');
}

export function loadUsageByAccount() {
  return getJSON('/usage/by-account');
}

export function loadProxySlots() {
  return getJSON('/proxy/slots');
}

/** 某个版本的可用模型，账号的排除名单从这里选。 */
export function loadRealmModels(realm) {
  return getJSON('/v1/models?realm=' + encodeURIComponent(realm));
}

export function setDisabledModels(uid, models) {
  return postJSON('/accounts/set', { uid, disabledModels: models });
}

/** 今天的活动流水：Buddy 加油站与每日活跃的完成情况都按它判定。 */
export function loadActivityToday() {
  return getJSON('/activity/history?range=today&limit=200');
}

export function refreshCredits(realm) {
  return postJSON('/accounts/credits', realm ? { realm } : {});
}

export function fetchCredits(uid) {
  return postJSON('/accounts/credits', { uid });
}

export function checkin(uid) {
  return postJSON('/accounts/checkin', uid ? { uid } : {});
}

/** 只给名单里的账号打卡：今天已经打过的不再打第二遍。 */
export function dailyChatPending(uids) {
  return postJSON('/accounts/daily-chat', { uids });
}

export function dailyChatWeb(uid) {
  return postJSON('/accounts/daily-chat-web', uid ? { uid } : {});
}

export function setEnabled(uid, enabled) {
  return postJSON('/accounts/set', { uid, enabled });
}

export function setPriority(uid, priority) {
  return postJSON('/accounts/set', { uid, priority });
}

export function setConcurrencyLimit(uid, concurrencyLimit) {
  return postJSON('/accounts/set', { uid, concurrencyLimit });
}

export function saveNote(uid, note) {
  return postJSON('/accounts/set', { uid, note });
}

export function setProxySlot(uid, proxySlot) {
  return postJSON('/accounts/set', { uid, proxySlot });
}

export function setProduct(uid, product) {
  return postJSON('/accounts/product', { uid, product });
}

export function setAllEnabled(enabled) {
  return postJSON('/accounts/set-all', { enabled });
}

export function testAccount(uid) {
  return postJSON('/accounts/test', { uid });
}

export function refreshAccountToken(uid) {
  return postJSON('/accounts/refresh', { uid });
}

export function deleteAccount(uid) {
  return postJSON('/accounts/delete', { uid });
}

/** 预览导入结果，服务端只校验不写盘。 */
export function importPreview(doc) {
  return postJSON('/accounts/import', { data: doc, dryRun: true });
}

export function importCommit(rows, overwrite) {
  return postJSON('/accounts/import', { data: rows, overwrite });
}

/** 只读扫描本机桌面客户端里的登录凭证。 */
export function scanDesktopCredentials() {
  return postJSON('/accounts/import/desktop', {});
}

export function importDesktopCredential(path, realm) {
  return postJSON('/accounts/import/desktop', { path, realm });
}

export function startLogin(realm, product) {
  return postJSON('/accounts/login/start', { product, realm });
}

export function pollLogin(state) {
  return getJSON('/accounts/login/poll?state=' + encodeURIComponent(state));
}

export function cancelLogin(state) {
  return postJSON('/accounts/login/cancel', { state });
}

function stamp() {
  return new Date().toISOString().slice(0, 19).replace(/[-:T]/g, '');
}

/** 导出账号文件；下载走面板会话，普通链接带不上会话请求头。传入 uid 时只导出该账号，文件名带账号前缀便于区分。 */
export function exportAccounts(uid = '') {
  const query = uid ? '?uid=' + encodeURIComponent(uid) : '';
  const label = uid ? uid.slice(0, 8) + '-' : '';
  return downloadFile('/accounts/export' + query, 'workbuddy-accounts-' + label + stamp() + '.json');
}
