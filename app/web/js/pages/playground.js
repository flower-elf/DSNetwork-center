import { html, render, nothing, repeat, live, ref, createRef } from '../core/dom.js';
import { icon } from '../core/icons.js';
import { getJSON, authHeaders } from '../core/api.js';
import { getState } from '../core/store.js';
import { fmtCredit, realmLabel } from '../core/format.js';
import { toast } from '../ui/toast.js';
import { confirmDialog } from '../ui/dialog.js';
import { pageHeader, notice, emptyState, loadErrorPage, loadErrorInline, busyClick } from '../ui/widgets.js';
import { accountHeader, accountOptions, buildChatBody, canSend, hasCreditRecord, parseJson, sessionCredit, usageParts, withAnswer } from './playground/logic.js';
import { streamChat } from './playground/stream.js';

// 测试台页：用面板会话直接调试 /v1/chat/completions，请求会真实消耗账号积分；流式分片累积在 playground/logic.js，SSE 解析在 playground/stream.js。

let seq = 0;

export function mount(host, ctx) {
  const realm = getState().viewRealm;
  const state = {
    models: null,        // null 表示还没读到模型列表
    modelsError: '',
    model: '',
    accounts: null,      // null 表示还没读到账号列表
    accountsError: '',
    account: '',         // 空串表示全部账号，由网关按调度规则挑选
    system: '',
    messages: [],
    input: '',
    streaming: false,
    controller: null,
  };
  const chatRef = createRef();
  const inputRef = createRef();
  const sendRef = createRef();
  let scheduled = false;

  /** /v1 的请求要自己带面板会话与 X-Realm 头；会话真的失效时借一次面板请求让外壳切回登录页。 */
  async function panelFetch(path, init = {}) {
    const response = await fetch(path, {
      cache: 'no-store',
      ...init,
      headers: authHeaders({ 'X-Realm': realm, ...(init.headers || {}) }),
    });
    return response;
  }

  /** 上游也可能返回 401；只有面板会话失效时外壳才会切到登录页，这里做一次判定。 */
  async function probeSession() {
    try {
      await getJSON('/panel/models?realm=' + encodeURIComponent(realm));
    } catch {
      // 会话失效时外壳已经切到登录页，这里不再重复提示
    }
  }

  async function loadModels() {
    try {
      const response = await panelFetch('/v1/models?realm=' + encodeURIComponent(realm));
      const text = await response.text();
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) await probeSession();
        throw new Error('读取模型列表失败，HTTP ' + response.status);
      }
      const reply = parseJson(text) || {};
      state.models = reply.data || [];
      state.modelsError = '';
      if (!state.models.some((m) => m.id === state.model)) {
        state.model = state.models.length ? state.models[0].id : '';
      }
    } catch (err) {
      state.models = null;
      state.modelsError = err.message || String(err);
    }
    draw();
  }

  async function loadAccounts() {
    try {
      const response = await panelFetch('/accounts?realm=' + encodeURIComponent(realm));
      if (!response.ok) {
        if (response.status === 401 || response.status === 403) await probeSession();
        throw new Error('读取账号列表失败，HTTP ' + response.status);
      }
      state.accounts = (await response.json()).accounts || [];
      state.accountsError = '';
      // 选中的账号已经不可用（停用、冷却中）时回到全部账号，免得下一次发送撞在一条已经过期的选择上
      if (state.account && !accountOptions(state.accounts).some((o) => o.value === state.account)) {
        state.account = '';
      }
    } catch (err) {
      state.accounts = null;
      state.accountsError = err.message || String(err);
    }
    draw();
  }

  async function load() {
    await Promise.all([loadModels(), loadAccounts()]);
  }

  async function send() {
    const text = state.input.trim();
    if (!text || state.streaming || !state.model) return;
    const history = [...state.messages, { id: ++seq, role: 'user', content: text }];
    const answerId = ++seq;
    state.messages = [...history, { id: answerId, role: 'assistant', content: '', reasoning: '',
                                    usage: null, error: '', streaming: true, elapsedMs: 0 }];
    state.input = '';
    state.streaming = true;
    const index = state.messages.length - 1;
    const controller = new AbortController();
    state.controller = controller;
    const startedAt = Date.now();
    const body = buildChatBody({ model: state.model, systemPrompt: state.system, messages: history });
    draw();
    try {
      const answer = await streamChat({
        url: '/v1/chat/completions',
        headers: authHeaders({ 'X-Realm': realm, ...accountHeader(state.account) }),
        body,
        signal: controller.signal,
        onUpdate: (partial) => {
          state.messages = withAnswer(state.messages, index, partial, Date.now() - startedAt);
          scheduleDraw();
        },
      });
      state.messages = withAnswer(state.messages, index, { ...answer, streaming: false }, Date.now() - startedAt);
      if (answer.stopped) {
        const partial = !!(answer.content || answer.reasoning);
        toast.info('已终止本次回答', partial ? '已经收到的内容保留在对话里' : '上游还没有返回内容');
      }
    } catch (err) {
      const status = err && err.status ? err.status : 0;
      state.messages = withAnswer(state.messages, index, { error: err.message || String(err), streaming: false }, Date.now() - startedAt);
      if (status === 401 || status === 403) await probeSession();
    } finally {
      state.streaming = false;
      state.controller = null;
      draw();
    }
  }

  function stop() {
    if (state.controller) state.controller.abort();
  }

  function clearChat() {
    confirmDialog({
      title: '清空对话？',
      desc: '将删除这一页的全部消息与本次会话的用量统计。测试台不保存历史记录，清空之后不能恢复。',
      confirmText: '清空',
      danger: true,
      onConfirm: () => { state.messages = []; },
    }).then((done) => {
      if (done) {
        toast.success('对话已清空');
        draw();
      }
    });
  }

  function onKeydown(event) {
    if (event.key !== 'Enter' || event.shiftKey) return;
    // 输入法组词过程中的回车用来选词，不算发送
    if (event.isComposing || event.keyCode === 229) return;
    event.preventDefault();
    send();
  }

  function bubble(m) {
    const isUser = m.role === 'user';
    const parts = usageParts(m.usage, m.elapsedMs);
    return html`
      <div class="pg-msg ${isUser ? 'user' : 'assistant'}">
        <div class="pg-avatar">${icon(isUser ? 'User' : 'Bot')}</div>
        <div class="pg-bubble">
          ${m.reasoning ? html`
            <details class="pg-reasoning">
              <summary>思考过程 <span class="muted">· 共 ${m.reasoning.length} 字</span></summary>
              <div class="pg-reasoning-body">${m.reasoning}</div>
            </details>` : nothing}
          ${m.content ? html`<div class="pg-text">${m.content}</div>` : nothing}
          ${m.streaming && !m.content && !m.error ? html`<div class="pg-waiting">${icon('LoaderCircle', 'spin')}正在等待上游返回内容…</div>` : nothing}
          ${m.error ? html`<div class="pg-error">${icon('CircleAlert')}<div class="grow break-all">${m.error}</div></div>` : nothing}
          ${m.stopped && m.content ? html`<div class="pg-usage">${icon('Ban')}已终止，以上是已经收到的内容</div>` : nothing}
          ${parts.length ? html`<div class="pg-usage">${parts.join(' · ')}</div>` : nothing}
        </div>
      </div>`;
  }

  function conversation() {
    if (state.messages.length) {
      return html`<div class="pg-chat-inner">${repeat(state.messages, (m) => m.id, bubble)}</div>`;
    }
    return html`
      <div class="pg-chat-inner">
        ${emptyState({
          icon: 'MessageSquare',
          title: '开始调试',
          desc: '请求会经本网关转给上游账号，回答支持流式输出，每条回答的消耗积分标在气泡下方。',
        })}
        <div class="pg-footnotes">
          <div>${icon('Info')}<span>这一页供调试使用：走管理端登录态，不受 API Key 与来源 IP 管控。</span></div>
          <div>${icon('KeyRound')}<span>正式接入请到「密钥」页签发 API Key，交给客户端调用。</span></div>
          <div>${icon('TriangleAlert')}<span>不支持的思考档位会由上游自动降级；回答可能包含推理内容。</span></div>
        </div>
      </div>`;
  }

  function composer() {
    const models = state.models || [];
    const creditRecorded = hasCreditRecord(state.messages);
    const disabled = !canSend({ model: state.model, text: state.input, streaming: state.streaming });
    return html`
      <form class="pg-composer" @submit=${(e) => { e.preventDefault(); send(); }}>
        <div class="pg-composer-fields">
          <label class="pg-field">
            <span class="field-label">模型</span>
            <select class="select" aria-label="调试的模型" .value=${live(state.model)}
                    @change=${(e) => { state.model = e.target.value; draw(); }}>
              ${models.map((m) => html`<option value=${m.id}>${m.name || m.id}</option>`)}
            </select>
          </label>
          <label class="pg-field">
            <span class="field-label">账号</span>
            <select class="select" aria-label="调试使用的账号" .value=${live(state.account)}
                    title="默认由网关按调度规则挑选账号；选定一个账号后，这次调试只用它。"
                    @change=${(e) => { state.account = e.target.value; draw(); }}>
              <option value="">全部账号</option>
              ${accountOptions(state.accounts).map((o) => html`<option value=${o.value}>${o.label}</option>`)}
            </select>
          </label>
          <label class="pg-field grow">
            <span class="field-label">系统提示词</span>
            <input class="input" type="text" placeholder="可选；留空时由网关补默认系统提示词"
                   .value=${live(state.system)} @input=${(e) => { state.system = e.target.value; }}>
          </label>
          ${creditRecorded ? html`
            <span class="pg-credit" title="本次会话累计消耗的积分，来自上游返回的 usage.credit">
              ${icon('Coins')}本次会话累计消耗 ${fmtCredit(sessionCredit(state.messages))} 积分
            </span>` : nothing}
        </div>
        <div class="pg-composer-main">
          <textarea class="textarea pg-input" rows="1" aria-label="要发送的内容" ${ref(inputRef)}
                    placeholder="输入要发送的内容，回车发送，Shift+回车换行"
                    .value=${live(state.input)}
                    @input=${(e) => { state.input = e.target.value; syncComposer(); }} @keydown=${onKeydown}></textarea>
          ${state.streaming
            ? html`<button type="button" class="btn btn-primary pg-send" @click=${stop}>${icon('Square')}停止</button>`
            : html`<button type="submit" class="btn btn-primary pg-send" ${ref(sendRef)} ?disabled=${disabled}>${icon('Send')}发送</button>`}
        </div>
        <div class="row-between text-2xs muted">
          <span>回车发送，Shift+回车换行</span>
          <span>${state.streaming ? '正在接收流式输出…' : ''}</span>
        </div>
      </form>`;
  }

  function view() {
    const actions = html`
      <button class="btn btn-outline btn-sm" title="重新读取这个版本可用的模型与账号" @click=${busyClick(load)}>${icon('RefreshCw')}刷新</button>
      <button class="btn btn-outline btn-sm" ?disabled=${!state.messages.length} @click=${clearChat}>${icon('Eraser')}清空对话</button>`;
    const head = pageHeader({
      path: 'playground',
      title: '聊天测试台',
      desc: `用${realmLabel(realm)}账号调试，请求会真实消耗账号积分`,
      actions,
    });
    if (state.models === null && state.modelsError) {
      return html`<div class="page">${head}${loadErrorPage(state.modelsError, loadModels)}</div>`;
    }
    return html`
      <div class="page pg-page">
        ${head}
        ${notice('info', html`请求会真实消耗账号积分：调试用的是面板里已经登录的账号，和客户端调用共用同一套账号池；默认由网关按调度规则挑选账号，也可以在「账号」里指定只用某一个。正式接入请在「密钥」页签发 API Key。`)}
        ${state.modelsError ? loadErrorInline(state.modelsError, loadModels) : nothing}
        ${state.accountsError ? loadErrorInline(state.accountsError, loadAccounts) : nothing}
        ${state.models !== null && state.models.length === 0
          ? notice('warn', html`没有可用的模型：请到「模型」页确认网关正在处理哪些模型，然后点「刷新」。`)
          : nothing}
        <div class="pg-chat scroll-slim" ${ref(chatRef)}>
          ${state.models === null
            ? html`<div class="pg-chat-inner"><span class="skel" style="height:140px"></span></div>`
            : conversation()}
        </div>
        ${composer()}
      </div>`;
  }

  function scheduleDraw() {
    if (scheduled) return;
    scheduled = true;
    requestAnimationFrame(() => {
      scheduled = false;
      draw();
    });
  }

  /** 输入过程中不重绘整页，只同步输入框高度与发送按钮的可用状态：这一页没有心跳，不在这里跟着输入改，按钮会一直是灰的。 */
  function syncComposer() {
    const el = inputRef.value;
    if (el) {
      el.style.height = 'auto';
      el.style.height = Math.min(132, Math.max(28, el.scrollHeight)) + 'px';
    }
    const send = sendRef.value;
    if (send) send.disabled = !canSend({ model: state.model, text: state.input, streaming: state.streaming });
  }

  function draw() {
    const chat = chatRef.value;
    const stick = chat ? chat.scrollHeight - chat.scrollTop - chat.clientHeight < 60 : true;
    render(view(), host);
    const box = chatRef.value;
    if (box && stick && state.messages.length) box.scrollTop = box.scrollHeight;
    syncComposer();
  }

  draw();
  load();
  return {
    refresh: () => load(),
    unmount() {
      if (state.controller) state.controller.abort();
    },
  };
}
