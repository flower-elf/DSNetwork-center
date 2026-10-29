// 设置 → 运行信息：版本、目录、设置文件、接口认证状态与 API 地址。
import { html } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { copyButton } from '../../ui/widgets.js';
import { aboutRows } from './logic.js';

const GITHUB_URL = 'https://github.com/flower-elf/DSNetwork-center';

export function createAboutTab(page) {
  return {
    render() {
      const rows = aboutRows(page.state.view || {}, location.origin);
      return html`
        <div class="card">
          <div class="card-head">
            <h2 class="card-title">${icon('Info')}运行信息</h2>
            <a class="btn btn-outline btn-sm" href=${GITHUB_URL} target="_blank" rel="noopener noreferrer">
              ${icon('Github')}GitHub 项目仓库
            </a>
          </div>
          <div class="about-rows">
            ${rows.map((row) => html`
              <div class="about-row">
                <span class="about-key">${row.label}</span>
                <span class="about-value ${row.mono ? 'mono' : ''} break-all">${row.value}</span>
                ${row.copy && row.value && row.value !== '—' ? copyButton(row.value, { title: '复制' + row.label }) : null}
              </div>`)}
          </div>
          <p class="text-2xs muted" style="margin-top:12px">
            客户端接入：Base URL 填上面的 API 地址，密钥用「密钥」页创建的 Key。
            OpenAI SDK 读取 OPENAI_BASE_URL 与 OPENAI_API_KEY，Claude Code 读取 ANTHROPIC_BASE_URL 与 ANTHROPIC_AUTH_TOKEN。
          </p>
        </div>`;
    },
  };
}
