/* 更新状态的纯判断：发布页链接只放行 GitHub，提示条件必须同时给出可用标记与版本号。
 * 运行：node tests/javascript/_test_update_logic.js
 */
import assert from 'node:assert/strict';

import { releaseUrl, updateAvailable } from '../../app/web/js/core/update.js';

assert.equal(
  releaseUrl({ release_url: 'https://github.com/flower-elf/DSNetwork-center/releases/tag/v1.2.3' }),
  'https://github.com/flower-elf/DSNetwork-center/releases/tag/v1.2.3',
  'GitHub 发布页原样保留');
assert.equal(releaseUrl({ release_url: ' https://github.com/x ' }), 'https://github.com/x',
  '两端空白先去掉再判断');
assert.equal(releaseUrl({ release_url: 'https://evil.example/x' }), '', '非 GitHub 地址一律丢弃');
assert.equal(releaseUrl({ release_url: 'javascript:alert(1)' }), '', '脚本地址不能进弹窗');
assert.equal(releaseUrl({ release_url: '' }), '');
assert.equal(releaseUrl({}), '');
assert.equal(releaseUrl(null), '');
assert.equal(
  releaseUrl({ release_url: '', releases_url: 'https://github.com/flower-elf/DSNetwork-center/releases' }),
  'https://github.com/flower-elf/DSNetwork-center/releases',
  '没有单条发布链接时退回发布页总入口');
assert.equal(releaseUrl({ release_url: 'https://evil.example/x',
                          releases_url: 'https://github.com/flower-elf/DSNetwork-center/releases' }),
  'https://github.com/flower-elf/DSNetwork-center/releases',
  '坏链接跳过，继续看兜底');

assert.equal(updateAvailable({ update_available: true, latest_version: '1.2.3' }), true);
assert.equal(updateAvailable({ update_available: true }), false, '没有版本号不弹窗');
assert.equal(updateAvailable({ update_available: false, latest_version: '1.2.3' }), false);
assert.equal(updateAvailable({ latest_version: '1.2.3' }), false);
assert.equal(updateAvailable({}), false);
assert.equal(updateAvailable(null), false);

console.log('update logic assertions passed');
