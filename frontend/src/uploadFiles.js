export function createUploadSessionId(random = globalThis.crypto) {
  // getRandomValues works on HTTP intranet pages; randomUUID requires HTTPS.
  return [...random.getRandomValues(new Uint8Array(16))].map(value => value.toString(16).padStart(2, '0')).join('');
}

export function directoryEntries(files) {
  const entries = files.map(file => ({ file, path: (file.webkitRelativePath || file.name).replaceAll('\\', '/') }));
  const root = entries[0]?.path.split('/')[0];
  // The selected directory itself is the source root, just as in server-directory intake.
  if (root && files.every(file => file.webkitRelativePath) && entries.every(entry => entry.path.startsWith(`${root}/`))) {
    for (const entry of entries) entry.path = entry.path.slice(root.length + 1);
  }
  const paths = new Set();
  for (const entry of entries) {
    const key = entry.path.toLowerCase();
    if (paths.has(key)) throw new Error(`目录中存在重复路径：${entry.path}`);
    paths.add(key);
  }
  return entries;
}

export async function uploadBatches(entries, send, options = {}) {
  const maxFiles = options.maxFiles || 20, maxBytes = options.maxBytes || 8 * 1024 * 1024;
  let offset = options.start || 0;
  let budget = maxFiles;
  while (offset < entries.length) {
    if (options.cancelled?.()) return;
    let end = offset, bytes = 0;
    while (end < entries.length && end - offset < budget) {
      const size = entries[end].file.size;
      if (end > offset && bytes + size > maxBytes) break;
      bytes += size; end++;
    }
    const batch = entries.slice(offset, end);
    let data, error;
    for (let attempt = 0; attempt < 3; attempt++) {
      if (options.cancelled?.()) return;
      try { data = await send(batch); error = null; break; }
      catch (err) {
        error = err;
        const retry = err instanceof TypeError || [408, 429, 502, 503, 504].includes(err.status);
        if (!retry || attempt === 2) break;
        await (options.wait || (ms => new Promise(resolve => setTimeout(resolve, ms))))(300 * (attempt + 1));
      }
    }
    if (error) {
      if (error.status === 413 && batch.length > 1) { budget = Math.max(1, Math.floor(batch.length / 2)); continue; }
      const reason = error.status === 413 ? '这个文件超过服务端单次请求大小限制，可改用服务端本地目录摄入' : error.message;
      const failure = new Error(`第 ${offset + 1}–${end} 个文件上传失败：${reason}。已完成 ${offset}/${entries.length}，可继续上传剩余文件。`);
      failure.completedFiles = offset;
      throw failure;
    }
    if (options.cancelled?.()) return;
    offset = end;
    budget = maxFiles;
    await options.onBatch?.(data, offset, entries.length);
  }
}
