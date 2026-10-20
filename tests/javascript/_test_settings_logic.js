/* 设置页纯逻辑：Messages 路径与模型下拉、数字与密码校验、费用估算表单、代理槽编辑器与绑定、运行信息。
 * 运行：node tests/javascript/_test_settings_logic.js
 */
import assert from 'node:assert/strict';

import * as logic from '../../app/web/js/pages/settings/logic.js';

// ---- Messages 接口翻译：下拉永远停在一个可提交的路径上
const formats = logic.MESSAGES_FORMATS.map((f) => f.value);
assert.equal(formats.length, 2, '只有两条翻译路径');
assert.equal(logic.messagesFormatValue('openai-responses', 'openai-completions'), 'openai-responses',
  '服务端存了哪条就选哪条');
assert.equal(logic.messagesFormatValue('', 'openai-responses'), 'openai-responses',
  '服务端没存过时用它给的默认值');
assert.equal(logic.messagesFormatValue('nonsense', 'openai-completions'), 'openai-completions',
  '存的值不在枚举里时退回默认值，不能把未知值提交回去');
assert.ok(formats.includes(logic.messagesFormatValue('', '')), '服务端什么都没给也要有一个可提交的值，不能是空串');

const picked = logic.messagesFormatOptions('openai-responses', 'openai-completions');
assert.deepEqual(picked.map((o) => o.value), formats);
assert.deepEqual(picked.map((o) => o.selected), [false, true], '预选的必须是服务端存的那条');
assert.ok(picked.every((o) => o.label.includes(o.value)), '选项文字要能认出对应哪条路径');
assert.deepEqual(logic.messagesFormatOptions('', 'openai-completions').map((o) => o.selected), [true, false]);

// ---- 测试使用的模型：目录之外的存值要补一条并预选
const options = logic.testModelOptions(['glm-5.3', 'deepseek-v4.1-flash'], '');
assert.equal(options[0].value, '', '首项是「默认」，值必须是空串');
assert.deepEqual(options.slice(1).map((o) => o.value), ['glm-5.3', 'deepseek-v4.1-flash']);
assert.ok(options.every((o) => !o.current), '存的是空值时没有「当前设置」项');
assert.deepEqual(options.map((o) => o.selected), [true, false, false],
  '存的是空值时要预选「默认」: ' + JSON.stringify(options));

const inCatalog = logic.testModelOptions(['glm-5.3', 'deepseek-v4.1-flash'], 'deepseek-v4.1-flash');
assert.deepEqual(inCatalog.map((o) => o.selected), [false, false, true],
  '存着的模型在目录里时要预选它，而不是退回「默认」: ' + JSON.stringify(inCatalog));

const kept = logic.testModelOptions(['glm-5.3'], 'retired-model');
assert.deepEqual(kept.map((o) => o.value), ['', 'glm-5.3', 'retired-model'],
  '目录里没有的旧值要补在最后，不能被悄悄换掉: ' + JSON.stringify(kept));
assert.equal(kept[2].current, true);
assert.ok(kept[2].label !== 'retired-model', '补出来的那项要有标记，好认出这是存着的旧值');

const known = logic.testModelOptions(['glm-5.3', 'glm-5.3', '  '], 'glm-5.3');
assert.deepEqual(known.map((o) => o.value), ['', 'glm-5.3'], '目录里的重复项与空项要去掉，已有的值不再补一条');

assert.equal(logic.testModelSelected(undefined), '', '没给值时用默认');
assert.equal(logic.testModelSelected('  glm-5.3  '), 'glm-5.3');

assert.equal(logic.testModelStateText('', 'deepseek-v4.1-flash').tone, 'muted', '用默认模型时不该标成自定义');
assert.ok(logic.testModelStateText('', 'deepseek-v4.1-flash').text.includes('deepseek-v4.1-flash'),
  '要写出实际会用哪个默认模型');
assert.equal(logic.testModelStateText('glm-5.3', 'deepseek-v4.1-flash').tone, 'info');
assert.ok(logic.testModelStateText('glm-5.3', 'deepseek-v4.1-flash').text.includes('glm-5.3'));
assert.equal(logic.testModelStateText('deepseek-v4.1-flash', 'deepseek-v4.1-flash').tone, 'muted',
  '存着的值就是默认值时按默认显示');

for (const good of ['glm-5.3', 'gpt-6-astra', 'a', 'A'.repeat(64), 'a_b-c.d']) {
  assert.equal(logic.validateTestModel(good), '', good + ' 应当被接受');
}
for (const bad of ['glm 5.3', 'intl/glm-5.3', '-lead', '.lead', '_lead', 'a'.repeat(65), '模型']) {
  assert.notEqual(logic.validateTestModel(bad), '', JSON.stringify(bad) + ' 上游不会接受，要本地拦下来');
}
assert.equal(logic.validateTestModel(''), '', '清空表示回到默认模型，必须允许保存');

// ---- 数字校验：空值表示不限
assert.deepEqual(logic.parseNonNegativeInteger(''), { ok: true, value: 0 });
assert.deepEqual(logic.parseNonNegativeInteger('0'), { ok: true, value: 0 });
assert.deepEqual(logic.parseNonNegativeInteger(' 120 '), { ok: true, value: 120 });
assert.equal(logic.parseNonNegativeInteger('-1').ok, false);
assert.equal(logic.parseNonNegativeInteger('1.5').ok, false);
assert.equal(logic.parseNonNegativeInteger('abc').ok, false);
assert.equal(logic.parseNonNegativeInteger('999', { max: 100 }).ok, false, '超过上限要拦下来');
assert.equal(logic.parseNonNegativeInteger('100', { max: 100 }).ok, true);

assert.deepEqual(logic.parseNonNegativeNumber(''), { ok: true, value: 0 });
assert.deepEqual(logic.parseNonNegativeNumber('0.5'), { ok: true, value: 0.5 });
assert.equal(logic.parseNonNegativeNumber('-0.1').ok, false);
assert.equal(logic.parseNonNegativeNumber('1e3').ok, false, '科学计数法不算普通数字');
assert.equal(logic.parseNonNegativeNumber('abc').ok, false);

// ---- 面板密码：服务端要求至少 4 位
assert.notEqual(logic.validatePasswordChange('', 'abcdef'), '', '没填当前密码要拦下来');
assert.notEqual(logic.validatePasswordChange('admin', ''), '', '没填新密码要拦下来');
assert.notEqual(logic.validatePasswordChange('admin', 'abc'), '', '新密码短于 4 位要拦下来');
assert.equal(logic.validatePasswordChange('admin', 'abcd'), '');
assert.equal(logic.validatePasswordChange('admin', 'admin123'), '');

// ---- 代理槽编辑器：未保存的改动不能被轮询覆盖
const baseline = [
  { id: 'a', name: '香港', url: 'http://127.0.0.1:17890', enabled: true, bound: 2 },
  { id: 'b', name: '国内', url: 'http://127.0.0.1:17891', enabled: false, bound: 0 },
];
assert.equal(logic.slotsDirty(baseline, baseline), false);
assert.equal(logic.slotsDirty(baseline.map((s) => ({ ...s })), baseline), false, '原样的副本不算改动（轮询要能过）');
assert.equal(logic.slotsDirty(logic.addSlot(baseline), baseline), true, '刚加的行还没保存，轮询不能把它冲掉');
assert.equal(logic.slotsDirty(logic.removeSlot(baseline, 0), baseline), true);
assert.equal(logic.slotsDirty(baseline.map((s) => ({ ...s, url: s.url + '/' })), baseline), true, '改了地址要算未保存');
assert.equal(logic.slotsDirty(baseline.map((s) => ({ ...s, enabled: !s.enabled })), baseline), true, '改了启用状态要算未保存');
assert.equal(logic.slotsDirty([baseline[1], baseline[0]], baseline), true, '顺序变了要算未保存');
assert.equal(logic.slotsDirty(baseline, []), true, '服务端清空后编辑器里的旧内容要算未保存');

const added = logic.addSlot(baseline);
assert.equal(added.length, 3);
assert.equal(added[0], baseline[0], '加行不能动已有的行');
assert.equal(added[2].url, '', '新行的地址是空的，等操作员填写');
assert.equal(added[2].enabled, true);
assert.notEqual(added[2].name, added[1].name, '新行的名字要与已有的区分开，免得看不出填的是哪一行');
assert.deepEqual(logic.removeSlot(baseline, 1).map((s) => s.id), ['a']);
assert.equal(logic.removeSlot(baseline, 5).length, 2, '下标越界时什么都不删');

assert.equal(logic.validateSlots([]), '', '空列表可以保存（表示不用代理）');
const blankRow = logic.validateSlots([{ id: '', name: 'x', url: '  ' }]);
assert.ok(blankRow.includes('1'), '地址空着要拦下来并指出第几行（服务端会直接丢掉这一行）: ' + blankRow);
assert.notEqual(logic.validateSlots([{ url: '127.0.0.1:7890' }]), '', '少了协议头的地址要拦下来');
assert.equal(logic.validateSlots([{ url: 'http://127.0.0.1:7890' }]), '');

const payload = logic.slotPayload([{ id: 'a', name: ' 香港 ', url: ' http://127.0.0.1:17890 ', enabled: false, bound: 3, exit_ip: '1.2.3.4' }]);
assert.deepEqual(payload, [{ id: 'a', name: '香港', url: 'http://127.0.0.1:17890', enabled: false }],
  '提交的字段要正好是服务端认的那几个: ' + JSON.stringify(payload));

const candidates = [
  { url: 'http://127.0.0.1:17890', reachable: true },
  { url: 'http://127.0.0.1:17891', reachable: false },
  { url: 'http://127.0.0.1:17892', reachable: true },
];
const imported = logic.importCandidates(baseline, candidates, ['http://127.0.0.1:17890', 'http://127.0.0.1:17892']);
assert.deepEqual(imported.map((s) => s.url), [baseline[0].url, baseline[1].url, 'http://127.0.0.1:17892'],
  '已经在列表里的地址不重复加，只加勾选的: ' + JSON.stringify(imported));
const discoverText = logic.discoverText(candidates);
assert.ok(discoverText.includes('3') && discoverText.includes('2'),
  '探测结果要写明总数与可达数量: ' + discoverText);

// ---- 绑定与自动分配
const accounts = [
  { uid: 'u1', enabled: true, proxySlot: '' },
  { uid: 'u2', enabled: true, proxySlot: 'a' },
  { uid: 'u3', enabled: false, proxySlot: '' },
  { uid: 'u4', enabled: true, proxySlot: '' },
];
assert.deepEqual(logic.unboundEnabledAccounts(accounts).map((a) => a.uid), ['u1', 'u4'],
  '只统计启用中且没有绑定的账号');

const slots = [
  { id: 'a', enabled: true, url: 'http://127.0.0.1:17890' },
  { id: 'b', enabled: false, url: 'http://127.0.0.1:17891' },
  { id: '', enabled: true, url: '' },
  { id: 'c', enabled: true, url: 'http://127.0.0.1:17892' },
];
assert.deepEqual(logic.autoAssignPlan(accounts, slots), [{ uid: 'u1', slotId: 'a' }, { uid: 'u4', slotId: 'c' }],
  '停用的槽位和还没保存的槽位都不能分出去');
assert.deepEqual(logic.autoAssignPlan(accounts, [{ id: 'a', enabled: false }]), [], '没有可用槽位时什么都不做');
assert.deepEqual(logic.autoAssignPlan([], slots), []);
assert.deepEqual(logic.autoAssignPlan([{ uid: 'u1' }, { uid: 'u2' }], [{ id: 'a', enabled: true }]),
  [{ uid: 'u1', slotId: 'a' }, { uid: 'u2', slotId: 'a' }], '账号比槽位多时循环复用');
assert.ok(logic.bindingText(3).includes('3'));
assert.notEqual(logic.bindingText(0), logic.bindingText(1));

// ---- 运行信息
const about = logic.aboutRows({
  version: '1.2.3', accounts_dir: 'C:/data/accounts', usage_dir: 'C:/data/usage/',
  settings_file: 'C:/data/accounts/settings.json', auth_required: true, api_key_set: true,
}, 'http://127.0.0.1:8788/');
const row = (label) => about.find((r) => r.label === label);
assert.equal(about.length, 7);
assert.equal(row('API 地址').value, 'http://127.0.0.1:8788/v1', 'API 地址末尾不能带斜杠');
assert.equal(row('用量日志').value, 'C:/data/usage/usage.jsonl', '用量日志由用量目录推导，不能拼出双斜杠');
assert.ok(row('账号存储目录').copy && row('设置文件').copy, '目录与地址要能一键复制');
assert.ok(!row('当前版本').copy, '版本号没有复制的必要');
assert.equal(row('当前版本').value, '1.2.3');

const open = logic.aboutRows({ version: 'x', usage_dir: '', auth_required: false }, '');
assert.notEqual(open.find((r) => r.label === '接口认证').value, row('接口认证').value,
  '不校验 Key 时要说明，别让人以为还得配密钥');
assert.ok(!open.find((r) => r.label === '用量日志').value.includes('usage.jsonl'),
  '服务端没给用量目录时不要编一个路径出来');
assert.equal(open.find((r) => r.label === 'API 地址').value, '/v1', '没有来源地址时也要给出相对路径');

const authTexts = [
  logic.aboutRows({ api_keys: [{ id: 'k', enabled: true, status: 'ok' }] }, '').find((r) => r.label === '接口认证').value,
  logic.aboutRows({ api_keys: [{ id: 'k', enabled: false }] }, '').find((r) => r.label === '接口认证').value,
  logic.aboutRows({ auth_required: false }, '').find((r) => r.label === '接口认证').value,
];
assert.equal(new Set(authTexts).size, 3,
  '有生效密钥 / 没有生效密钥 / 接口不校验，三种状态要给出三种说明: ' + JSON.stringify(authTexts));
assert.ok(authTexts.every((text) => text && text.trim() !== ''), '认证状态不能空着');

const hours = logic.parseHours('21, 9 21; 05');
assert.deepEqual(hours, { ok: true, value: [5, 9, 21] }, '去重、排序，逗号/空格/分号都接受');
assert.equal(logic.hoursText(hours.value), '5, 9, 21');
assert.deepEqual(logic.parseHours(''), { ok: true, value: [] }, '留空表示这组不跑');
assert.equal(logic.parseHours('24').ok, false);
assert.equal(logic.parseHours('abc').ok, false);
assert.equal(logic.parseHours('-1').ok, false);

// ---- 费用估算：默认表、脏检查与提交载荷
const costState = logic.costEstimateState({
  credit_price: 0,
  models: [{ model: 'deepseek-v4.1-flash', input: 2, cached_input: 0.04, output: 8 }],
});
assert.equal(costState.creditPrice, '0');
assert.deepEqual(costState.rows, [{ model: 'deepseek-v4.1-flash', input: '2', cached_input: '0.04', output: '8' }]);
assert.equal(logic.costEstimateDirty(costState, costState), false, '原样的草稿不算脏');
assert.equal(logic.costEstimateDirty({ ...costState, creditPrice: '0.1' }, costState), true);

const costBuilt = logic.costEstimatePayload({
  creditPrice: '0.1',
  rows: [
    { model: 'deepseek-v4.1-flash', input: '2', cached_input: '0.04', output: '8' },
    { model: '', input: '0', cached_input: '0', output: '0' },
  ],
});
assert.equal(costBuilt.ok, true);
assert.deepEqual(costBuilt.payload, {
  cost_estimate: {
    credit_price: 0.1,
    models: [{ model: 'deepseek-v4.1-flash', input: 2, cached_input: 0.04, output: 8 }],
  },
}, '空行要丢掉，价格按数字提交');

const missingModel = logic.costEstimatePayload({
  creditPrice: '0', rows: [{ model: '', input: '1', cached_input: '0', output: '0' }],
});
assert.equal(missingModel.ok, false, '填了价格却没填模型名必须报错');
assert.ok(missingModel.errors.rows[0]);

const duplicate = logic.costEstimatePayload({
  creditPrice: '0',
  rows: [
    { model: 'glm-5.3', input: '1', cached_input: '0', output: '1' },
    { model: 'GLM-5.3', input: '2', cached_input: '0', output: '2' },
  ],
});
assert.equal(duplicate.ok, false, '模型名去重不区分大小写');
assert.ok(duplicate.errors.rows[1]);

const badPrice = logic.costEstimatePayload({
  creditPrice: '0', rows: [{ model: 'glm-5.3', input: '-1', cached_input: '0', output: '1' }],
});
assert.equal(badPrice.ok, false, '负价格不能提交');

const inheritCached = logic.costEstimatePayload({
  creditPrice: '0',
  rows: [{ model: 'glm-5.3', input: '2', cached_input: '', output: '8' }],
});
assert.equal(inheritCached.ok, true);
assert.equal(inheritCached.payload.cost_estimate.models[0].cached_input, 2,
  '缓存命中价留空时要按输入价提交');

const freeCached = logic.costEstimatePayload({
  creditPrice: '0',
  rows: [{ model: 'glm-5.3', input: '2', cached_input: '0', output: '8' }],
});
assert.equal(freeCached.payload.cost_estimate.models[0].cached_input, 0,
  '显式填写 0 表示缓存命中免费');

const zeroRow = logic.costEstimatePayload({
  creditPrice: '0',
  rows: [{ model: 'glm-5.3', input: '0', cached_input: '0', output: '0' }],
});
assert.equal(zeroRow.ok, true);
assert.deepEqual(zeroRow.payload.cost_estimate.models, [],
  '价格全为 0 的模型行不参与估算，也不存储');

console.log('settings logic assertions passed');
