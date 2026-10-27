// 设置 → 高级：功能开关保存在设置里，修改后立即生效。
import { html, nothing } from '../../core/dom.js';
import { icon } from '../../core/icons.js';
import { toast, toastError } from '../../ui/toast.js';

export function createAdvancedTab(page) {
  const form = {
    anthropicMessages: false,
    dshToolNeutralization: false,
    freeFallback: false,
    promptCacheKey: false,
    affinityDebug: false,
    upstreamKeepalive: true,
    usageCache: true,
    usageDayBuckets: true,
    recentIndex: false,
    egressBackoff: false,
  };
  let baseline = null;
  let saving = false;
  let dirtyShown = false;
  let envOverrides = {};

  // 表单字段是驼峰，设置接口用下划线；环境变量清单按接口字段上报。
  const ENV_KEYS = {
    anthropicMessages: 'enable_anthropic_messages',
    dshToolNeutralization: 'dsh_tool_neutralization',
    freeFallback: 'free_model_credit_fallback',
    promptCacheKey: 'prompt_cache_key',
    affinityDebug: 'affinity_debug',
    upstreamKeepalive: 'upstream_keepalive',
    usageCache: 'usage_cache',
    usageDayBuckets: 'usage_day_buckets',
    recentIndex: 'recent_index',
  };

  function stateOf(view) {
    return {
      anthropicMessages: view.enable_anthropic_messages === true,
      dshToolNeutralization: view.dsh_tool_neutralization === true,
      freeFallback: view.free_model_credit_fallback === true,
      promptCacheKey: view.prompt_cache_key === true,
      affinityDebug: view.affinity_debug === true,
      upstreamKeepalive: view.upstream_keepalive !== false,
      usageCache: view.usage_cache !== false,
      usageDayBuckets: view.usage_day_buckets !== false,
      recentIndex: view.recent_index === true,
      egressBackoff: view.egress_backoff_enabled === true,
    };
  }

  function applyState(state) {
    Object.assign(form, state);
    dirtyShown = false;
  }

  function seed(view) {
    baseline = stateOf(view);
    applyState(baseline);
    envOverrides = (view && view.env_overrides) || {};
  }

  function isDirty() {
    if (!baseline) return false;
    return Object.keys(form).some((key) => form[key] !== baseline[key]);
  }

  function markDirty() {
    if (isDirty() === dirtyShown) return;
    dirtyShown = isDirty();
    page.draw();
  }

  function revert() {
    if (!baseline) return;
    applyState(baseline);
    page.draw();
    toast.info('已撤销未保存的改动');
  }

  async function saveAll() {
    saving = true;
    page.draw();
    try {
      const view = await page.save({
        enable_anthropic_messages: form.anthropicMessages,
        dsh_tool_neutralization: form.dshToolNeutralization,
        free_model_credit_fallback: form.freeFallback,
        prompt_cache_key: form.promptCacheKey,
        affinity_debug: form.affinityDebug,
        upstream_keepalive: form.upstreamKeepalive,
        usage_cache: form.usageCache,
        usage_day_buckets: form.usageDayBuckets,
        recent_index: form.recentIndex,
        egress_backoff_enabled: form.egressBackoff,
      });
      baseline = stateOf(view);
      applyState(baseline);
      toast.success('高级设置已保存');
      return true;
    } catch (err) {
      toastError('保存高级设置失败', err);
      return false;
    } finally {
      saving = false;
      page.draw();
    }
  }

  function switchRow({ name, desc, field }) {
    const env = envOverrides[ENV_KEYS[field] || field];
    return html`
      <div class="setting">
        <div class="setting-main">
          <div class="setting-name">${name}</div>
          <div class="setting-desc">${desc}</div>
          ${env ? html`<div class="field-hint tone-warning">环境变量 ${env.name} = ${env.value}，优先于面板设置</div>` : nothing}
        </div>
        <div class="setting-control">
          <label class="switch">
            <input type="checkbox" .checked=${form[field]} ?disabled=${saving} aria-label=${name}
                   @change=${(e) => { form[field] = e.target.checked; markDirty(); }}>
            <span></span>
          </label>
        </div>
      </div>`;
  }

  return {
    seed,
    render() {
      const dirty = isDirty();
      return html`
        <div class="card">
          <div class="card-head">
            <div>
              <h2 class="card-title">${icon('Wrench')}高级</h2>
              <div class="card-sub">
                这些开关保存在设置里，修改后立即生效。存在同名环境变量时以环境变量为准。
                <span class="text-2xs ${dirty ? 'tone-warning' : 'muted'}">${dirty ? '(有未保存的改动)' : ''}</span>
              </div>
            </div>
            <div class="row-wrap">
              <button class="btn btn-outline btn-sm" ?disabled=${saving || !dirty} @click=${revert}>
                ${icon('Undo2')}撤销
              </button>
              <button class="btn btn-primary btn-sm" ?disabled=${saving || !dirty} @click=${saveAll}>
                ${saving ? icon('LoaderCircle', 'spin') : icon('Save')}保存
              </button>
            </div>
          </div>
          <div class="setting-list">
            <div class="section-label">接口与身份</div>
            ${switchRow({
              name: 'Anthropic Messages 入口',
              field: 'anthropicMessages',
              desc: '打开 /v1/messages 入口。网关会把 Anthropic 请求翻译成 Chat 或 Responses 请求，cache_control 会被忽略，长会话可能明显增加积分消耗。默认关闭。',
            })}
            ${switchRow({
              name: '对 DSH 工具调用进行处理',
              field: 'dshToolNeutralization',
              desc: '通用化工具调用，避免问题。仅作用于 Chat Completions，默认关闭。',
            })}
            ${switchRow({
              name: '亲和调试日志',
              field: 'affinityDebug',
              desc: '派生会话标识时把消息条数与页内轮转写入运行日志。默认关闭，仅用于排查。',
            })}
            ${switchRow({
              name: 'prompt_cache_key',
              field: 'promptCacheKey',
              desc: '向上游附带 prompt_cache_key。实测该上游会自行缓存前缀，这个字段没有改变命中率或积分消耗，仅保留作实验开关。默认关闭。',
            })}
            <div class="section-label">免费额度</div>
            ${switchRow({
              name: '免费模型积分回退',
              field: 'freeFallback',
              desc: '免费模型额度耗尽时回退到同系列的付费模型，会消耗积分。默认关闭。',
            })}
            <div class="section-label">调度与失败处理</div>
            ${switchRow({
              name: '出口级瞬时失败退避',
              field: 'egressBackoff',
              desc: '传输类失败与 502/503/504 记在出口上并做秒级退避，不再计入账号级熔断。默认关闭。',
            })}
            <div class="section-label">性能与缓存</div>
            ${switchRow({
              name: '上游连接复用',
              field: 'upstreamKeepalive',
              desc: '复用上游 TCP/TLS 连接，省掉每轮请求的握手。默认开启；关闭后回退到每次新建连接，行为保持兼容。',
            })}
            ${switchRow({
              name: '用量聚合缓存',
              field: 'usageCache',
              desc: '把用量聚合状态写入磁盘，重启后只读新增日志。默认开启；关闭后每次重启都重新扫描日志。',
            })}
            ${switchRow({
              name: '日与小时分桶',
              field: 'usageDayBuckets',
              desc: '窗口统计使用按日/小时分桶的增量结果。默认开启；关闭后回退到逐行扫描，结果相同但更慢。',
            })}
            ${switchRow({
              name: '最近请求索引',
              field: 'recentIndex',
              desc: '为请求日志建立深分页索引，减少大偏移翻页的读取量。默认关闭；打开后会占用少量磁盘。',
            })}
          </div>
        </div>`;
    },
    async canLeave() {
      if (!isDirty()) return true;
      const choice = await page.confirmUnsaved({
        desc: '「高级」里的改动还没有保存。',
        onSave: saveAll,
      });
      return choice !== 'cancel';
    },
  };
}
