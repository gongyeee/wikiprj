function evidencePath(item) {
  return item.path || item.filePath || item.filepath || item.relativePath || item.id || "";
}

export function evidenceSelectionKey(item) {
  if (item.chunkId !== undefined && item.chunkId !== null && item.chunkId !== "") return `chunk:${item.chunkId}`;
  if (item.structuralNodeId) return `node:${evidencePath(item)}:${item.structuralNodeId}`;
  const path = evidencePath(item);
  return path ? `path:${path}` : "";
}

export function selectedEvidencePayload(results, selectedKeys) {
  const selected = new Set(selectedKeys);
  const items = results.filter(item => selected.has(evidenceSelectionKey(item)));
  return {
    paths: [...new Set(items.map(evidencePath).filter(Boolean))],
    chunkIds: [...new Set(items.flatMap(item => item.chunkIds?.length ? item.chunkIds : [item.chunkId]).filter(value => value !== undefined && value !== null && value !== ""))],
    count: items.length,
  };
}

export function uniqueEvidenceResults(results) {
  const seen = new Set();
  return results.filter(item => {
    const key = evidenceSelectionKey(item);
    if (!key || seen.has(key)) return false;
    seen.add(key);
    return true;
  });
}
