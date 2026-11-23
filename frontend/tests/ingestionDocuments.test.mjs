import test from 'node:test';
import assert from 'node:assert/strict';
import { buildIngestionPlan } from '../src/ingestionDocuments.js';

const documents = [{ key: 'new:A', name: '文档A' }, { key: 'existing:B', id: 'existing:B', name: '文档B' }];
const rows = () => [
  { sourceFile: '文档A_1.md', selected: true, documentKey: 'new:A', versionLabel: '1', setDefault: false },
  { sourceFile: '文档A_2.md', selected: true, documentKey: 'new:A', versionLabel: '2', setDefault: true },
  { sourceFile: '其他.md', selected: false, documentKey: '', versionLabel: '', setDefault: false }
];

test('摄入时把两个文件明确归入同一文档，并保留各自版本与默认版选择', () => {
  const plan = buildIngestionPlan(rows(), documents);
  assert.deepEqual(plan.sourceFiles, ['文档A_1.md', '文档A_2.md']);
  assert.deepEqual(plan.sourceAssignments, [
    { sourceFile: '文档A_1.md', documentName: '文档A', versionLabel: '1', setDefault: false },
    { sourceFile: '文档A_2.md', documentName: '文档A', versionLabel: '2', setDefault: true }
  ]);
});
test('已有文档使用持久ID，未选择的文件不提交归属', () => {
  const input = rows();
  input[0].documentKey = 'existing:B';
  input[1].selected = false;
  assert.deepEqual(buildIngestionPlan(input, documents).sourceAssignments, [
    { sourceFile: '文档A_1.md', logicalDocumentId: 'existing:B', versionLabel: '1', setDefault: false }
  ]);
});
test('全不选不能意外触发全目录摄入，重复版本和多个默认版不能提交', () => {
  assert.throws(() => buildIngestionPlan([], documents), /至少选择/);
  const input = rows();
  input[1].versionLabel = ' 1 ';
  assert.throws(() => buildIngestionPlan(input, documents), /版本号.*重复/);
  input[1].versionLabel = '2';
  input[0].setDefault = true;
  assert.throws(() => buildIngestionPlan(input, documents), /一个默认版本/);
  input[0].setDefault = false;
  input[0].versionLabel = ' ';
  assert.throws(() => buildIngestionPlan(input, documents), /填写.*版本号/);
});
