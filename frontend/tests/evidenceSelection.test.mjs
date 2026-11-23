import assert from "node:assert/strict";
import test from "node:test";

import { evidenceSelectionKey, selectedEvidencePayload, uniqueEvidenceResults } from "../src/evidenceSelection.js";

test("同一文档的不同 chunk 可以独立选择并精确提交", () => {
  const results = [
    { path: "raw/sources/rules.docx", chunkId: 11 },
    { path: "raw/sources/rules.docx", chunkId: 12 },
    { path: "raw/sources/other.docx", chunkId: 13 },
  ];
  const firstKey = evidenceSelectionKey(results[0]);

  assert.notEqual(firstKey, evidenceSelectionKey(results[1]));
  assert.deepEqual(results.map(item => [firstKey].includes(evidenceSelectionKey(item))), [true, false, false]);
  assert.deepEqual(selectedEvidencePayload(results, [firstKey]), {
    paths: ["raw/sources/rules.docx"],
    chunkIds: [11],
    count: 1,
  });
});

test("重复证据只显示一次，分组证据提交全部 chunk", () => {
  const grouped = { path: "raw/sources/rules.docx", chunkId: 11, chunkIds: [11, 12] };
  assert.equal(uniqueEvidenceResults([grouped, { ...grouped }]).length, 1);
  assert.deepEqual(selectedEvidencePayload([grouped], [evidenceSelectionKey(grouped)]).chunkIds, [11, 12]);
});
