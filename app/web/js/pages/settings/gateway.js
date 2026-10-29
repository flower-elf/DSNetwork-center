// 设置 → 网关行为：保留积分、每日限额、自动切换、打卡通道、本地网络工具、Messages 翻译路径与接口测试模型；控件先改本地草稿，由卡片上的「保存」统一提交。
import { html, live, nothing, repeat } from '../../core/dom.js';
import { getJSON } from '../../core/api.js';
import { toast, toastError } from '../../ui/toast.js';
import {
  hoursText,
  messagesFormatOptions,
  messagesFormatValue,
  parseHours,
  parseNonNegativeInteger,
  testModelOptions,
  testModelSelected,
  testModelStateText,
  validateTestModel,
} from './logic.js';
import { numberRow, settingCard, switchRow } from './rows.js';

const SCHEDULE_FIELDS = ('checkin travel keepalive cat').split(' ');
const SCHEDULE_LABELS = {
  checkin: '签到时段',
  travel: '猫猫旅行时段',
  keepalive: '保活时段',
  cat: '夜猫任务时段',
};

export function createGatewayTab(page) {
  const form = {
    reserve: '0',
    dailyLimit: '0',
    dailyCreditLimit: '0',
    modelDailyTokenLimit: '0',
    expiringWindow: '7',
    dailyWebRetry: true,
    dailyWebAttempts: '3',
    dailyWebTimeout: '120',
    dailyChatModel: '',
    taskJitter: '5',
    schedule: { checkin: '9, 21', travel: '9, 21', keepalive: '22', cat: '1' },
    smartRouting: true,
    sessionAffinity: true,
    autoSwitch: false,
    dailyWeb: true,
    localWeb: false,
    messagesFormat: 'openai-completions',
    testModel: { intl: '', cn: '' },
  };
  const errors = { reserve: '', dailyLimit: '', dailyCreditLimit: '',
                   modelDailyTokenLimit: '', expiringWindow: '',
                   dailyWebAttempts: '', dailyWebTimeout: '', dailyChatModel: '',
                   taskJitter: '',
                   schedule: { checkin: '', travel: '', keepalive: '', cat: '' },
                   testModel: { intl: '', cn: '' } };
  // 服务端最后一次给出的设置：撤销回到这里，也是「有未保存的改动」的对照。
  let baseline = null;
  // 表头的未保存标记只在翻转时重绘，输入过程中不打断光标位置。
  let dirtyShown = false;
  let saving = false;
  let catalogs = { intl: [], cn: [] };
  let catalogError = '';

  function expiringWindowValue(view) {
    const flat = view && view.expiring_window_days;
    const nested = (((view || {}).limits || {}).expiring_window_days || {}).global;
    const raw = flat !== undefined && flat !== null ? flat : nested;
    const number = Number(raw);
    return String(Number.isFinite(number) ? number : 7);
  }

  function stateOf(view) {
    return {
      reserve: String(Number(view.reserve_credits) || 0),
      dailyLimit: String(Number(view.daily_token_limit) || 0),
      dailyCreditLimit: String(Number(view.daily_credit_limit) || 0),
      modelDailyTokenLimit: String(Number(view.model_daily_token_limit) || 0),
      expiringWindow: expiringWindowValue(view),
      dailyWebRetry: view.daily_chat_web_retry !== false,
      dailyWebAttempts: String(Number(view.daily_chat_web_attempts) || 3),
      dailyWebTimeout: String(Number(view.daily_chat_web_timeout) || 120),
      dailyChatModel: String(view.daily_chat_model || ''),
      taskJitter: String(Number(view.task_jitter_max) || 5),
      schedule: Object.fromEntries(SCHEDULE_FIELDS.map((field) => [
        field, hoursText((view.scheduler_hours || {})[field]),
      ])),
      smartRouting: view.smart_routing !== false,
      sessionAffinity: view.session_affinity !== false,
      autoSwitch: view.auto_switch_product === true,
      dailyWeb: view.daily_chat_web !== false,
      localWeb: view.local_web_tools === true,
      messagesFormat: messagesFormatValue(view.messages_format, view.messages_format_default),
      testModel: {
        intl: testModelSelected(view.test_model_intl),
        cn: testModelSelected(view.test_model_cn),
      },
    };
  }

  function applyState(state) {
    form.reserve = state.reserve;
    form.dailyLimit = state.dailyLimit;
    form.dailyCreditLimit = state.dailyCreditLimit;
    form.modelDailyTokenLimit = state.modelDailyTokenLimit;
    form.expiringWindow = state.expiringWindow;
    form.dailyWebRetry = state.dailyWebRetry;
    form.dailyWebAttempts = state.dailyWebAttempts;
    form.dailyWebTimeout = state.dailyWebTimeout;
    form.dailyChatModel = state.dailyChatModel;
    form.taskJitter = state.taskJitter;
    for (const field of SCHEDULE_FIELDS) form.schedule[field] = state.schedule[field];
    form.smartRouting = state.smartRouting;
    form.sessionAffinity = state.sessionAffinity;
    form.autoSwitch = state.autoSwitch;
    form.dailyWeb = state.dailyWeb;
    form.localWeb = state.localWeb;
    form.messagesFormat = state.messagesFormat;
    form.testModel.intl = state.testModel.intl;
    form.testModel.cn = state.testModel.cn;
    errors.reserve = '';
    errors.dailyLimit = '';
    errors.dailyCreditLimit = '';
    errors.modelDailyTokenLimit = '';
    errors.dailyWebAttempts = '';
    errors.dailyWebTimeout = '';
    errors.dailyChatModel = '';
    errors.expiringWindow = '';
    errors.taskJitter = '';
    for (const field of SCHEDULE_FIELDS) errors.schedule[field] = '';
    errors.testModel.intl = '';
    errors.testModel.cn = '';
    dirtyShown = false;
  }

  /** 用服务端的值填满控件，换标签或重新进入时调用。 */
  function seed(view) {
    baseline = stateOf(view);
    applyState(baseline);
  }

  function isDirty() {
    if (!baseline) return false;
    return String(form.reserve).trim() !== baseline.reserve
      || String(form.dailyLimit).trim() !== baseline.dailyLimit
      || String(form.dailyCreditLimit).trim() !== baseline.dailyCreditLimit
      || String(form.modelDailyTokenLimit).trim() !== baseline.modelDailyTokenLimit
      || String(form.expiringWindow).trim() !== baseline.expiringWindow
      || form.dailyWebRetry !== baseline.dailyWebRetry
      || String(form.dailyWebAttempts).trim() !== baseline.dailyWebAttempts
      || String(form.dailyWebTimeout).trim() !== baseline.dailyWebTimeout
      || String(form.dailyChatModel).trim() !== baseline.dailyChatModel
      || String(form.taskJitter).trim() !== baseline.taskJitter
      || SCHEDULE_FIELDS.some((field) =>
        String(form.schedule[field]).trim() !== baseline.schedule[field])
      || form.smartRouting !== baseline.smartRouting
      || form.sessionAffinity !== baseline.sessionAffinity
      || form.autoSwitch !== baseline.autoSwitch
      || form.dailyWeb !== baseline.dailyWeb
      || form.localWeb !== baseline.localWeb
      || form.messagesFormat !== baseline.messagesFormat
      || form.testModel.intl !== baseline.testModel.intl
      || form.testModel.cn !== baseline.testModel.cn;
  }

  function structChanged() {
    if (isDirty() === dirtyShown) return;
    dirtyShown = isDirty();
    page.draw();
  }

  /** 撤销：控件全部退回服务端最后一次给出的设置。 */
  function revert() {
    if (!baseline) return;
    applyState(baseline);
    page.draw();
    toast.info('已撤销未保存的改动');
  }

  async function loadCatalogs() {
    try {
      const [intl, cn] = await Promise.all([
        getJSON('/v1/models?realm=intl'),
        getJSON('/v1/models?realm=cn'),
      ]);
      const ids = (payload) => ((payload && payload.data) || []).map((m) => m.id).filter(Boolean);
      catalogs = { intl: ids(intl), cn: ids(cn) };
      catalogError = '';
    } catch (err) {
      // 目录读取失败不影响其它设置项；下拉退回只有「默认」一项并说明原因。
      catalogError = err.message;
    }
  }

  /** 校验并提交整张卡片；返回是否保存成功。 */
  async function saveAll() {
    const settingsView = page.state.view || {};
    const reserve = parseNonNegativeInteger(form.reserve, { label: '保留积分' });
    const dailyLimit = parseNonNegativeInteger(form.dailyLimit, { label: '每日 Token 限额' });
    const dailyCredit = parseNonNegativeInteger(form.dailyCreditLimit, { label: '每日积分限额' });
    const modelDaily = parseNonNegativeInteger(form.modelDailyTokenLimit, { label: '单模型每日 Token 限额' });
    const expiring = parseNonNegativeInteger(form.expiringWindow, {
      label: '临期积分优先窗口', max: 365,
    });
    const webAttempts = parseNonNegativeInteger(form.dailyWebAttempts, {
      label: '网页通道尝试次数',
      max: Number(settingsView.daily_chat_web_attempts_max) || 10,
    });
    const webTimeout = parseNonNegativeInteger(form.dailyWebTimeout, {
      label: '网页通道单轮超时',
      max: Number(settingsView.daily_chat_web_timeout_max) || 600,
    });
    const jitter = parseNonNegativeInteger(form.taskJitter, { label: '任务随机间隔', max: 600 });
    errors.reserve = reserve.ok ? '' : reserve.error;
    errors.dailyLimit = dailyLimit.ok ? '' : dailyLimit.error;
    errors.dailyCreditLimit = dailyCredit.ok ? '' : dailyCredit.error;
    errors.modelDailyTokenLimit = modelDaily.ok ? '' : modelDaily.error;
    errors.expiringWindow = expiring.ok ? '' : expiring.error;
    errors.dailyWebAttempts = !webAttempts.ok ? webAttempts.error
      : (webAttempts.value < 1 ? '网页通道尝试次数不能小于 1' : '');
    errors.dailyWebTimeout = !webTimeout.ok ? webTimeout.error
      : (webTimeout.value < 1 ? '网页通道单轮超时不能小于 1 秒' : '');
    form.dailyChatModel = String(form.dailyChatModel || '').trim();
    errors.dailyChatModel = validateTestModel(form.dailyChatModel);
    errors.taskJitter = !jitter.ok ? jitter.error
      : (jitter.value < 2 ? '任务随机间隔不能小于 2 秒' : '');
    const schedule = {};
    for (const field of SCHEDULE_FIELDS) {
      const parsed = parseHours(form.schedule[field], { label: SCHEDULE_LABELS[field] });
      errors.schedule[field] = parsed.ok ? '' : parsed.error;
      if (parsed.ok) schedule[field] = parsed.value;
    }
    for (const realm of ['intl', 'cn']) {
      form.testModel[realm] = String(form.testModel[realm] || '').trim();
      errors.testModel[realm] = validateTestModel(form.testModel[realm]);
    }
    if (errors.reserve || errors.dailyLimit || errors.dailyCreditLimit
        || errors.modelDailyTokenLimit || errors.expiringWindow
        || errors.dailyWebAttempts || errors.dailyWebTimeout || errors.dailyChatModel
        || errors.taskJitter
        || SCHEDULE_FIELDS.some((field) => errors.schedule[field])
        || errors.testModel.intl || errors.testModel.cn) {
      page.draw();
      return false;
    }
    saving = true;
    page.draw();
    try {
      const view = await page.save({
        reserve_credits: reserve.value,
        daily_token_limit: dailyLimit.value,
        daily_credit_limit: dailyCredit.value,
        model_daily_token_limit: modelDaily.value,
        expiring_window_days: expiring.value,
        daily_chat_web_retry: form.dailyWebRetry,
        daily_chat_web_attempts: webAttempts.value,
        daily_chat_web_timeout: webTimeout.value,
        daily_chat_model: form.dailyChatModel,
        task_jitter_max: jitter.value,
        scheduler_hours: schedule,
        smart_routing: form.smartRouting,
        session_affinity: form.sessionAffinity,
        auto_switch_product: form.autoSwitch,
        daily_chat_web: form.dailyWeb,
        local_web_tools: form.localWeb,
        messages_format: form.messagesFormat,
        test_model_intl: form.testModel.intl,
        test_model_cn: form.testModel.cn,
      });
      baseline = stateOf(view);
      applyState(baseline);
      toast.success('网关行为设置已保存');
      return true;
    } catch (err) {
      toastError('保存网关行为设置失败', err);
      return false;
    } finally {
      saving = false;
      page.draw();
    }
  }

  // 字段行共用的表单状态：这一组没有环境变量覆盖，改动先清错误再按翻转情况重绘。
  const ctl = {
    form,
    get saving() { return saving; },
    errors,
    change: (field, value) => {
      form[field] = value;
      if (field in errors) errors[field] = '';
      structChanged();
    },
  };

  /** 排程时段输入行：逗号分隔的整点，状态放在 schedule 分组里。 */
  function hoursRow({ name, desc, field }) {
    return html`
      <div class="setting">
        <div class="setting-main">
          <div class="setting-name">${name}</div>
          <div class="setting-desc">${desc}</div>
          ${errors.schedule[field] ? html`<div class="field-error">${errors.schedule[field]}</div>` : nothing}
        </div>
        <div class="setting-control">
          <input class="input input-hours" type="text" inputmode="numeric" placeholder="例如 9, 21"
                 .value=${live(form.schedule[field])} ?disabled=${saving}
                 @input=${(e) => { form.schedule[field] = e.target.value; errors.schedule[field] = ''; structChanged(); }}>
          <span class="setting-unit">整点</span>
        </div>
      </div>`;
  }

  function testModelRow() {
    const view = page.state.view || {};
    const cell = (realm) => {
      const label = realm === 'cn' ? '国内版' : '国际版';
      const fallback = realm === 'cn' ? view.test_model_cn_default : view.test_model_intl_default;
      const stateText = testModelStateText(form.testModel[realm], fallback);
      const options = testModelOptions(catalogs[realm], form.testModel[realm]);
      return html`
        <div class="test-model-cell">
          <div class="test-model-head">
            <span class="test-model-realm">${label}</span>
            <span class="text-2xs ${stateText.tone === 'info' ? 'tone-info' : 'muted'}">${stateText.text}</span>
          </div>
          <select class="select input-sm select-wide" .value=${live(form.testModel[realm])} ?disabled=${saving}
                  @change=${(e) => { form.testModel[realm] = e.target.value; errors.testModel[realm] = ''; structChanged(); }}>
            ${repeat(options, (opt) => opt.value, (opt) => html`<option value=${opt.value} ?selected=${opt.selected}>${opt.label}</option>`)}
          </select>
          ${errors.testModel[realm] ? html`<div class="field-error">${errors.testModel[realm]}</div>` : nothing}
        </div>`;
    };
    return html`
      <div class="setting">
        <div class="setting-main">
          <div class="setting-name">接口测试模型</div>
          <div class="setting-desc">
            「测试」按钮发送 hi 对话时使用的模型；「默认」由网关按出口选择。
          </div>
        </div>
        <div class="setting-control test-model-row">
          ${cell('intl')}
          ${cell('cn')}
        </div>
      </div>`;
  }

  return {
    seed,
    render() {
      const view = page.state.view || {};
      const messagesOptions = messagesFormatOptions(form.messagesFormat, view.messages_format_default);
      return settingCard({
        iconName: 'Sliders',
        title: '网关行为',
        desc: '改动后点「保存」提交，点「撤销」回到上一次保存的设置。',
        dirty: isDirty(),
        saving,
        onRevert: revert,
        onSave: saveAll,
        body: html`
          <div class="setting-list">
            <div class="section-label">限额与配额</div>
            ${numberRow(ctl, {
              name: '保留积分', field: 'reserve', suffix: '积分',
              desc: '账号余额低于或等于该值时暂停接单，填写 0 表示关闭。与每日 Token 限额各自独立，任一命中都会暂停接单。',
            })}
            ${numberRow(ctl, {
              name: '每日 Token 限额', field: 'dailyLimit', suffix: 'token', step: '1000',
              desc: '账号当日消耗的 token 达到该值后暂停接单，请求自动切到其他账号，本地时间 0 点后恢复。填写 0 表示不限额。',
            })}
            ${numberRow(ctl, {
              name: '每日积分限额', field: 'dailyCreditLimit', suffix: '积分',
              desc: '账号当日消耗的积分达到该值后，付费模型暂停接单；免费模型照常服务。填写 0 表示不限额。',
            })}
            ${numberRow(ctl, {
              name: '单模型每日 Token 限额', field: 'modelDailyTokenLimit', suffix: 'token', step: '1000',
              desc: '同一账号在同一模型上当日消耗的 token 达到该值后，只暂停这个模型，账号的其他模型照常接单。填写 0 表示不限额。',
            })}
            ${numberRow(ctl, {
              name: '临期积分优先窗口',
              field: 'expiringWindow',
              suffix: '天',
              desc: '积分在这个天数内到期时优先分配请求，默认 7 天；填写 0 表示关闭。智能分配与轮询都使用这个窗口。',
            })}
            <div class="section-label">定时任务</div>
            ${numberRow(ctl, {
              name: '任务随机间隔',
              field: 'taskJitter',
              suffix: '秒',
              min: 2,
              desc: '定时巡检在处理每个账号前随机等待 2 秒到这个值之间的时间，避免同一批请求同时发出。默认 5 秒，最大 600 秒。',
            })}
            ${numberRow(ctl, {
              name: '网页通道尝试次数',
              field: 'dailyWebAttempts',
              suffix: '次',
              min: 1,
              desc: '国际版网页通道打卡一轮最多尝试几次。默认 3 次；失败后会重新创建会话。',
            })}
            ${numberRow(ctl, {
              name: '网页通道单轮超时',
              field: 'dailyWebTimeout',
              suffix: '秒',
              min: 1,
              desc: '国际版网页通道打卡一轮最多等待多少秒。默认 120 秒。',
            })}
            <div class="setting">
              <div class="setting-main">
                <div class="setting-name">国际版每日活跃模型</div>
                <div class="setting-desc">网页通道打卡使用哪个模型。留空使用内置模型；只影响每日活跃打卡。</div>
                ${errors.dailyChatModel ? html`<div class="field-error">${errors.dailyChatModel}</div>` : nothing}
              </div>
              <div class="setting-control">
                <input class="input input-sm" type="text" placeholder="留空使用内置模型"
                       aria-label="国际版每日活跃模型"
                       .value=${live(form.dailyChatModel)} ?disabled=${saving}
                       @input=${(e) => { form.dailyChatModel = e.target.value; errors.dailyChatModel = ''; structChanged(); }}>
              </div>
            </div>
            ${hoursRow({
              name: '签到与活跃上报时段',
              field: 'checkin',
              desc: '国内版签到与对话活跃上报的整点，默认 9, 21。填写 0 到 23，用逗号分隔；留空表示这组不跑。',
            })}
            ${hoursRow({
              name: '猫猫旅行时段',
              field: 'travel',
              desc: '猫猫旅行派出与领取的整点，默认 9, 21。',
            })}
            ${hoursRow({
              name: 'Token 保活时段',
              field: 'keepalive',
              desc: '检查并刷新即将过期令牌的整点，默认 22。',
            })}
            ${hoursRow({
              name: '夜猫任务时段',
              field: 'cat',
              desc: '夜猫子专属任务的上报整点，默认 1。',
            })}
            <div class="section-label">调度</div>
            ${switchRow(ctl, {
              name: '会话粘性',
              field: 'sessionAffinity',
              desc: '同一段对话固定使用同一个账号，复用上游的前缀缓存；账号不能接单时才换号。客户端没有会话标识时按对话开头识别。关闭后每一轮请求都重新选择账号。',
            })}
            ${switchRow(ctl, {
              name: '智能分配',
              field: 'smartRouting',
              desc: '关闭时按轮询顺序分配新对话。打开时按积分到期节奏与最近一小时忙闲分配，剩余额度接近耗尽的账号与模型组合会略优先。已在服务的对话不受影响。',
            })}
            ${switchRow(ctl, {
              name: '429 自动切换出站身份',
              field: 'autoSwitch',
              desc: '某账号在某模型上收到上游 429 限流时，自动在 WB / VSC / CLI 之间换身份重试，同一账号与模型 60 秒内最多切换 4 次。切换后的身份会保存，重启后继续沿用。',
            })}
            <div class="section-label">每日活跃</div>
            ${switchRow(ctl, {
              name: '国际版每日活跃打卡网页通道',
              field: 'dailyWeb',
              desc: '国际版账号每日打卡时，除桌面端身份的轻量对话外，再走一次网页通道的会话。只有网页端对话计入官方的每日活跃奖励，开启后会消耗账号少量积分。',
            })}
            ${switchRow(ctl, {
              name: '网页通道失败后重试',
              field: 'dailyWebRetry',
              desc: '网页通道打卡一轮失败时，重新创建会话再试。关闭后只尝试一次。',
            })}
            <div class="section-label">网络工具</div>
            ${switchRow(ctl, {
              name: '本地网络工具（web_search / web_fetch）',
              field: 'localWeb',
              desc: '客户端声明的 web_search / web_fetch 服务端工具在本网关代为执行：搜索走 DuckDuckGo，抓取页面时读取模型给出的 URL。开启后网关会主动出网，每轮多请求一次上游。',
            })}
            <div class="section-label">接口</div>
            <div class="setting">
              <div class="setting-main">
                <div class="setting-name">Messages 接口翻译</div>
                <div class="setting-desc">
                  /v1/messages 把 Anthropic 请求翻译成哪种上游格式。默认使用旧版；客户端携带 Responses 风格的自定义工具时改用新版。
                </div>
              </div>
              <div class="setting-control">
                <select class="select input-sm select-wide" .value=${live(form.messagesFormat)} ?disabled=${saving}
                        @change=${(e) => { form.messagesFormat = e.target.value; structChanged(); }}>
                  ${messagesOptions.map((opt) => html`<option value=${opt.value} ?selected=${opt.selected}>${opt.label}</option>`)}
                </select>
              </div>
            </div>
            ${testModelRow()}
          </div>
          ${catalogError ? html`
            <p class="field-hint tone-warning">模型目录这次没有读取成功，下拉里只显示「默认」与已保存的值：${catalogError}</p>` : nothing}`,
      });
    },
    /** 有未保存的改动时先问一句：保存、放弃或取消这次跳转。 */
    async canLeave() {
      if (!isDirty()) return true;
      const choice = await page.confirmUnsaved({
        desc: '「网关行为」里的改动还没有保存。',
        onSave: saveAll,
      });
      return choice !== 'cancel';
    },
    onMount() {
      // 首次进入就要填满接口测试模型下拉：目录来自 /v1/models，读取失败只影响这一项。
      loadCatalogs().then(() => page.draw());
    },
  };
}
