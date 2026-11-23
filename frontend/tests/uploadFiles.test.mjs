import test from 'node:test';
import assert from 'node:assert/strict';
import { createUploadSessionId, directoryEntries, uploadBatches } from '../src/uploadFiles.js';

const folder = (count = 230) => Array.from({ length: count }, (_, index) => ({
  name: '规则.md', webkitRelativePath: `交易规则/目录${index}/子目录/规则.md`, size: 1024
}));

test('目录上传以所选目录为根，保留子目录，同名文件不混在一起', () => {
  const entries = directoryEntries(folder());
  assert.equal(entries[0].path, '目录0/子目录/规则.md');
  assert.equal(new Set(entries.map(entry => entry.path)).size, 230);
  assert.equal(directoryEntries([{ name: '规则.md', webkitRelativePath: '', size: 1 }])[0].path, '规则.md');
});
test('HTTP内网可生成稳定上传会话，不依赖HTTPS专用randomUUID', () => {
  assert.equal(createUploadSessionId({ getRandomValues(values) { return values.fill(10); } }), '0a'.repeat(16));
});
test('230份文件分成12批，每批不超过20份并保留路径及顺序', async () => {
  const calls = [], progress = [];
  await uploadBatches(directoryEntries(folder()), async batch => { calls.push(batch); return { ok: true }; }, {
    onBatch(data, completed) { progress.push(completed); }
  });
  assert.equal(calls.length, 12);
  assert.equal(calls.at(-1).length, 10);
  assert.equal(progress.at(-1), 230);
  assert.deepEqual(calls.flat().map(entry => entry.path), directoryEntries(folder()).map(entry => entry.path));
});
test('网络临时失败会重试，批次最终失败后从断点续传', async () => {
  const entries = directoryEntries(folder(45));
  let attempts = 0;
  await uploadBatches(entries.slice(0, 1), async () => { if (attempts++ === 0) throw new TypeError('network'); return {}; }, { wait: async () => {} });
  assert.equal(attempts, 2);
  let completed = 0;
  await assert.rejects(uploadBatches(entries, async batch => {
    if (batch[0].path.startsWith('目录20/')) throw Object.assign(new Error('disk unavailable'), { status: 400 });
    return {};
  }, { onBatch(data, value) { completed = value; } }), error => error.completedFiles === 20);
  assert.equal(completed, 20);
  const retried = [];
  await uploadBatches(entries, async batch => { retried.push(...batch); return {}; }, { start: completed });
  assert.deepEqual(retried.map(entry => entry.path), entries.slice(20).map(entry => entry.path));
});
test('请求过大时缩小批次，单个文件超限则明确提示替代入口', async () => {
  let count = 0;
  await uploadBatches(directoryEntries(folder(10)), async batch => {
    if (batch.length > 3) throw Object.assign(new Error('too large'), { status: 413 });
    count += batch.length; return {};
  });
  assert.equal(count, 10);
  await assert.rejects(uploadBatches(directoryEntries(folder(1)), async () => {
    throw Object.assign(new Error('too large'), { status: 413 });
  }), /服务端本地目录/);
});
