import test from 'node:test';
import assert from 'node:assert/strict';
import { pastedCases, parseTsv } from '../src/evaluationHelpers.js';

test('Excel 粘贴保留单元格内换行、制表符、引号和空字段', () => {
  const rows = pastedCases('问题\t预期答案\t分类\r\n第一题\t"第一点\n第二点\t带制表符和""引号"""\t业务\r\n第二题\t第二答\t\r\n');
  assert.equal(rows.length, 2);
  assert.equal(rows[0].expected, '第一点\n第二点\t带制表符和"引号"');
  assert.equal(rows[1].category, '');
});

test('错误粘贴格式不能静默写入', () => {
  assert.throws(() => parseTsv('题\t"未结束'), /未闭合/);
  assert.throws(() => pastedCases('a\tb\tc\td\te\tf\tg\th'), /超过 7 列/);
  assert.throws(() => pastedCases(''), /1～2000/);
});
