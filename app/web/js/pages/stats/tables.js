// 用量页的表格模板：账号用量、按密钥与模型性能矩阵。数据行由 stats/logic.js 算好，这里只拼模板。
import { html, nothing } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { fmtNumber, fmtPercent, fmtLatency, fmtDateTime } from '../../core/format.js';
import { realmBadge, emptyState } from '../../ui/widgets.js';
import { rangeLabel, matrixOptions, buildMatrix } from './logic.js';

/** 输入 / 输出 / 思考三段 Token 的一行小字。 */
export function tripleTokens(prompt, completion, reasoning) {
  return html`
    <span class="text-2xs nums">
      <span class="tone-info">输入 ${fmtNumber(prompt)}</span>
      <span class="muted"> · </span>
      <span class="tone-success">输出 ${fmtNumber(completion)}</span>
      <span class="muted"> · </span>
      <span class="tone-violet">思考 ${fmtNumber(reasoning)}</span>
    </span>`;
}

/** 性能列的首字延迟：均值与 P50 / P90 / P95 / P99 分位。 */
function ttftCell(stat) {
  if (!stat.ttftAvg) return '—';
  return html`
    <div>${fmtLatency(stat.ttftAvg)}</div>
    ${stat.ttftP50 != null ? html`<div class="muted text-2xs">P50 ${fmtLatency(stat.ttftP50)}</div>` : nothing}
    ${stat.ttftP90 != null ? html`<div class="muted text-2xs">P90 ${fmtLatency(stat.ttftP90)}</div>` : nothing}
    ${stat.ttftP95 != null ? html`<div class="muted text-2xs">P95 ${fmtLatency(stat.ttftP95)}</div>` : nothing}
    ${stat.ttftP99 != null ? html`<div class="muted text-2xs">P99 ${fmtLatency(stat.ttftP99)}</div>` : nothing}`;
}

export function accountTable(rows, range) {
  return html`
    <div class="card card-flush">
      <div class="card-head">
        <h2 class="card-title">各账号用量与模型消耗分布</h2>
        <span class="card-sub">${rows.length ? rows.length + ' 个账号' : '暂无数据'}</span>
      </div>
      ${rows.length === 0
        ? html`<div class="empty text-xs muted">${rangeLabel(range)}没有账号用量数据</div>`
        : html`
          <div class="table-wrap">
            <table class="table">
              <thead>
                <tr>
                  <th>账号</th>
                  <th>所属区域</th>
                  <th class="right">剩余积分</th>
                  <th class="right">请求次数</th>
                  <th class="right">消耗 Token</th>
                  <th class="right">消耗积分</th>
                  <th>调用的模型分布</th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((row) => html`
                  <tr>
                    <td>
                      <div class="medium truncate">${row.nickname}</div>
                      <div class="cell-mono">${row.uid.slice(0, 10)}</div>
                    </td>
                    <td>${row.realm ? realmBadge(row.realm) : nothing}</td>
                    <td class="right nums">${row.credits && row.credits.remain != null
                      ? html`<b>${fmtNumber(row.credits.remain)}</b>${row.credits.size ? html`<span class="muted text-2xs"> / ${fmtNumber(row.credits.size)}</span>` : nothing}`
                      : html`<span class="muted">—</span>`}</td>
                    <td class="right nums">${fmtNumber(row.requests)}</td>
                    <td class="right nums"><b>${fmtNumber(row.totalTokens)}</b></td>
                    <td class="right nums">${Number(row.credit).toFixed(2)}</td>
                    <td>
                      ${row.models.length === 0
                        ? html`<span class="muted text-2xs">无调用</span>`
                        : html`<div class="model-pills">${row.models.map((m) => html`
                            <span class="model-pill"><span class="cell-mono">${m.id}</span> ${fmtNumber(m.requests)} 次 · ${fmtNumber(m.tokens)} token</span>`)}</div>`}
                    </td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`}
    </div>`;
}

export function keyTable(rows, idle, range) {
  return html`
    <div class="card card-flush">
      <div class="card-head">
        <h2 class="card-title">${icon('KeyRound')}按密钥</h2>
        <span class="card-sub">每把密钥在当前范围内的调用与消耗，密钥名取自密钥列表</span>
      </div>
      ${rows.length === 0
        ? html`<div class="empty text-xs muted">${rangeLabel(range)}没有密钥调用数据</div>`
        : html`
          <div class="table-wrap">
            <table class="table">
              <thead>
                <tr>
                  <th>密钥</th>
                  <th class="right">请求次数</th>
                  <th class="right">失败</th>
                  <th class="right">消耗 Token</th>
                  <th class="right">消耗积分</th>
                  <th class="right">缓存命中率</th>
                  <th class="right">首字延迟</th>
                  <th>用量占比</th>
                </tr>
              </thead>
              <tbody>
                ${rows.map((row) => html`
                  <tr>
                    <td><div class="medium truncate">${row.keyName}</div><div class="cell-mono">${row.keyId}</div></td>
                    <td class="right nums">${fmtNumber(row.requests)}</td>
                    <td class="right nums">${row.errors ? html`<span class="tone-danger">${fmtNumber(row.errors)}</span>` : html`<span class="muted">0</span>`}</td>
                    <td class="right nums"><b>${fmtNumber(row.totalTokens)}</b></td>
                    <td class="right nums">${Number(row.credit).toFixed(2)}</td>
                    <td class="right nums">${row.cacheHitPct == null ? '—' : fmtPercent(row.cacheHitPct)}</td>
                    <td class="right nums">${row.ttftAvg ? fmtLatency(row.ttftAvg) : '—'}</td>
                    <td class="share-cell">
                      <span class="share-track"><span class="share-bar" style="width:${Math.max(3, row.share)}%"></span></span>
                      <span class="share-pct text-2xs muted nums">${row.share}%</span>
                    </td>
                  </tr>`)}
              </tbody>
            </table>
          </div>`}
      ${idle.length
        ? html`<div class="card-sub" style="padding:10px 16px 14px">另有 ${idle.length} 把密钥在这个范围没有调用：${idle.slice(0, 6).join('、')}${idle.length > 6 ? ' 等' : ''}。</div>`
        : nothing}
    </div>`;
}

function matrixCell(row, usage) {
  const names = (usage && usage.accounts_map) || {};
  if (row.acct) {
    const name = (names[row.acct] || {}).nickname || row.acct.slice(0, 8);
    return html`<b>${name}</b>`;
  }
  const mapped = Object.entries(row.accounts || {}).filter(([uid]) => uid && uid !== '(unattributed)');
  if (!mapped.length) return html`<span class="muted">全部账号</span>`;
  return html`<div class="model-pills">${mapped.map(([uid, count]) => html`
    <span class="badge badge-sm">${(names[uid] || {}).nickname || uid.slice(0, 6)} <span class="muted">${count}</span></span>`)}</div>`;
}

export function matrixTable({ usage, perf, filters, range, setFilter }) {
  const options = matrixOptions(usage);
  const built = usage ? buildMatrix(usage, perf, filters) : null;
  const rows = built ? built.rows : [];
  const head = html`
    <div class="card-head">
      <h2 class="card-title">${icon('Cpu')}模型性能指标与用量一览</h2>
      <div class="row-wrap">
        <span class="card-sub">${built ? (built.modelCount ? built.modelCount + ' 个模型，共 ' + rows.length + ' 行明细' : '暂无模型请求') : '未读取到'}</span>
        <label class="row text-2xs muted">账号
          <select class="select input-sm" @change=${(e) => setFilter({ account: e.target.value })}>
            <option value="" ?selected=${filters.account === ''}>全部账号</option>
            ${options.accounts.map((opt) => html`<option value=${opt.uid} ?selected=${filters.account === opt.uid}>${opt.label}</option>`)}
          </select>
        </label>
        <label class="row text-2xs muted">模型
          <select class="select input-sm" @change=${(e) => setFilter({ model: e.target.value })}>
            <option value="" ?selected=${filters.model === ''}>全部模型</option>
            ${options.models.map((id) => html`<option value=${id} ?selected=${filters.model === id}>${id}</option>`)}
          </select>
        </label>
      </div>
    </div>`;
  if (!built) return html`<div class="card">${head}<div class="empty text-xs muted">用量数据没有读取到，稍后自动重试</div></div>`;
  if (!rows.length) {
    return html`<div class="card">${head}${emptyState({ icon: 'Cpu', title: '暂无模型请求', desc: '当前范围与筛选条件下没有任何模型调用记录。' })}</div>`;
  }
  const summary = built.summary;
  return html`
    <div class="card card-flush">
      ${head}
      <div class="table-wrap">
        <table class="table">
          <thead>
            <tr>
              <th>模型名称</th>
              <th class="center">区域</th>
              <th>账号</th>
              <th class="right">请求数</th>
              <th class="right">失败</th>
              <th class="right">总 Token</th>
              <th class="matrix-opt">输入 / 输出 / 思考</th>
              <th class="right">首字延迟</th>
              <th class="right">生成速度</th>
              <th class="right matrix-opt">端到端耗时</th>
              <th class="right">缓存命中率</th>
              <th>用量占比</th>
            </tr>
          </thead>
          <tbody>
            <tr class="matrix-summary">
              <td><b>${summary.label}</b></td>
              <td class="center"><span class="muted">—</span></td>
              <td><span class="muted">${summary.accountsLabel}</span></td>
              <td class="right nums">${fmtNumber(summary.requests)}</td>
              <td class="right nums">${summary.errors ? html`<span class="tone-danger">${fmtNumber(summary.errors)}</span>` : html`<span class="muted">0</span>`}</td>
              <td class="right nums"><b>${fmtNumber(summary.totalTokens)}</b></td>
              <td class="matrix-opt">${tripleTokens(summary.promptTokens, summary.completionTokens, summary.reasoningTokens)}</td>
              <td class="right nums">${ttftCell(summary)}</td>
              <td class="right nums">${summary.tps ? Number(summary.tps).toFixed(1) + ' tok/s' : '—'}</td>
              <td class="right nums matrix-opt">${summary.wallMs ? fmtLatency(summary.wallMs) : '—'}</td>
              <td class="right nums">${summary.cachePct == null ? '—' : fmtPercent(summary.cachePct)}</td>
              <td class="share-cell"><span class="share-track"><span class="share-bar" style="width:100%"></span></span> <span class="share-pct text-2xs muted nums">100%</span></td>
            </tr>
            ${rows.map((row) => html`
              <tr>
                <td>
                  <div class="cell-mono">${row.id}</div>
                  ${row.split && row.first ? html`<div class="text-2xs muted nowrap">共 ${row.splitAccounts} 个账号参与此模型处理</div>` : nothing}
                </td>
                <td class="center">${row.realm ? realmBadge(row.realm) : html`<span class="muted">—</span>`}</td>
                <td>${matrixCell(row, usage)}</td>
                <td class="right nums">${fmtNumber(row.requests)}</td>
                <td class="right nums">${row.errors ? html`<span class="tone-danger">${fmtNumber(row.errors)}</span>` : html`<span class="muted">0</span>`}</td>
                <td class="right nums"><b>${fmtNumber(row.totalTokens)}</b></td>
                <td class="matrix-opt">${tripleTokens(row.promptTokens, row.completionTokens, row.reasoningTokens)}</td>
                <td class="right nums">${ttftCell(row)}</td>
                <td class="right nums">${row.tps ? Number(row.tps).toFixed(1) + ' tok/s' : '—'}</td>
                <td class="right nums matrix-opt">${row.wallMs ? fmtLatency(row.wallMs) : '—'}</td>
                <td class="right nums">${row.cachePct == null ? '—' : fmtPercent(row.cachePct)}</td>
                <td class="share-cell">
                  <span class="share-track"><span class="share-bar" style="width:${Math.max(3, row.share)}%"></span></span>
                  <span class="share-pct text-2xs muted nums">${row.share}%</span>
                </td>
              </tr>`)}
          </tbody>
        </table>
      </div>
      <div class="card-sub" style="padding:10px 16px 14px">
        首字延迟与生成速度取自最后一次性能采样，范围比采样更宽时这三列只覆盖较新的请求。
        ${perf && perf.sample_capped && perf.sample_from
          ? html`当前统计的是自 ${fmtDateTime(perf.sample_from)} 之后的部分请求。`
          : nothing}
      </div>
    </div>`;
}
