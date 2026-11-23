export function buildIngestionPlan(rows, documents) {
  const selected = rows.filter(row => row.selected);
  if (!selected.length) throw new Error('请至少选择一个要摄入的文件');
  const sourceAssignments = [];
  const labels = new Set();
  const defaults = new Set();
  for (const row of selected) {
    if (!row.documentKey) continue;
    const document = documents.find(item => item.key === row.documentKey);
    if (!document) throw new Error(`请为 ${row.sourceFile} 重新选择所属文档`);
    const version = row.versionLabel.trim();
    if (!version) throw new Error(`请填写 ${row.sourceFile} 的版本号`);
    const pair = JSON.stringify([document.key, version]);
    if (labels.has(pair)) throw new Error(`${document.name} 的版本号 ${version} 重复，请为不同文件填写不同版本号`);
    labels.add(pair);
    if (row.setDefault) {
      if (defaults.has(document.key)) throw new Error(`${document.name} 只能选择一个默认版本`);
      defaults.add(document.key);
    }
    sourceAssignments.push({
      sourceFile: row.sourceFile, versionLabel: version, setDefault: Boolean(row.setDefault),
      ...(document.id ? { logicalDocumentId: document.id } : { documentName: document.name })
    });
  }
  return {
    sourceFiles: selected.length === rows.length ? [] : selected.map(row => row.sourceFile),
    sourceAssignments
  };
}
