// 更新状态的纯判断：面板与弹窗共用，不碰 DOM，便于在 Node 里测试。

const RELEASE_PREFIX = 'https://github.com/';

/** 发布页链接：优先单条发布，其次仓库的发布总入口；只放行 GitHub 的 https 地址。 */
export function releaseUrl(status) {
  const candidates = [status && status.release_url, status && status.releases_url];
  for (const raw of candidates) {
    const url = String(raw || '').trim();
    if (url.startsWith(RELEASE_PREFIX)) return url;
  }
  return '';
}

/** 是否存在可提示的新版本：要求服务端明确给出 update_available 与版本号。 */
export function updateAvailable(status) {
  return !!(status && status.update_available && status.latest_version);
}
