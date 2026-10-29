// 新版本提示：登录面板与「立即检查」共用同一份文案和按钮。
import { html, nothing } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { releaseUrl } from '../core/update.js';
import { openDialog } from './dialog.js';

/** 发现新版本时的弹窗；调用方决定什么时候弹。 */
export function openUpdateDialog(status) {
  const url = releaseUrl(status);
  return openDialog({
    width: 480,
    icon: 'Sparkles',
    title: '发现新版本 ' + status.latest_version,
    desc: '当前 v' + (status.current_version || '?')
      + '，最新稳定版 v' + status.latest_version + '。',
    body: () => html`
      <div class="col">
        <p class="text-xs muted" style="margin:0">
          到发布页下载新版压缩包，解压后替换旧文件并重启网关。账号与用量数据在 accounts、usage 目录里，不会被覆盖。
        </p>
        ${status.published_at
          ? html`<p class="text-2xs muted" style="margin:0">发布时间：${status.published_at}</p>`
          : nothing}
      </div>`,
    foot: (ctl) => html`
      <button class="btn btn-ghost" @click=${() => ctl.close()}>稍后</button>
      ${url
        ? html`<a class="btn btn-primary" href=${url} target="_blank" rel="noopener noreferrer"
                   @click=${() => ctl.close()}>${icon('Download')}打开发布页</a>`
        : nothing}`,
  });
}
