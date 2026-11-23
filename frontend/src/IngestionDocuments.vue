<template>
  <section class="ingestion-documents">
    <div class="source-heading">
      <div><h3>1. 选择文件，设置文档归属</h3><p>例如：文档A_1 和 文档A_2 都选择“文档A”，版本号分别填写“1”和“2”。</p></div>
      <div class="actions compact">
        <label v-if="intakeMode !== 'server'" class="upload-button">选择目录添加<input type="file" webkitdirectory multiple :disabled="busy || pending" @change="upload" /></label>
        <label v-if="intakeMode !== 'server'" class="upload-button">选择文件添加<input type="file" multiple accept=".doc,.docx,.xlsx,.xls,.pptx,.pdf,.md,.txt,.csv,.html,.htm" :disabled="busy || pending" @change="upload" /></label>
        <button v-if="projectId || intakeMode === 'server'" type="button" @click="load" :disabled="busy || pending || (intakeMode === 'server' && !rawDir.trim())">{{ intakeMode === 'server' ? '读取服务端目录' : '读取项目已有目录' }}</button>
      </div>
    </div>
    <p class="source-help">支持 Word（DOC/DOCX）、Excel（XLSX/XLS）、PowerPoint、PDF、Markdown、TXT、CSV、HTML，统一转换为 Markdown 后摄入。</p>
    <p v-if="uploadSummary" class="source-help">本次选择 {{ uploadSummary.chosen }} 个文件，上传完成 {{ uploadSummary.uploaded }} 个，未上传 {{ uploadSummary.skipped }} 个。上传完成仅表示文件已接收；解析、抽取和实际入库结果在任务详情中显示。</p>
    <p v-if="pending || progress" class="source-help" role="status">{{ progress || '正在读取目录，请稍候…' }}</p>
    <p v-if="receivedTarget?.storageNotice" class="source-help">{{ receivedTarget.storageNotice }}</p>
    <div v-if="uploadQueue && !pending" class="actions compact"><button type="button" @click="continueUpload" :disabled="busy">继续上传剩余文件</button><button type="button" @click="cancelRemaining" :disabled="busy">取消剩余，保留已上传文件</button></div>
    <p v-if="error" role="alert" class="source-error">{{ error }}</p>
    <template v-if="loaded">
      <div class="source-toolbar">
        <span>共 {{ rows.length }} 个文件，已选择 {{ rows.filter(row => row.selected).length }} 个</span>
        <label for="new-ingestion-document">新文档名称</label>
        <input id="new-ingestion-document" v-model="newName" maxlength="200" placeholder="例如：文档A" :disabled="busy" @keydown.enter.prevent="addDocument" />
        <button type="button" @click="addDocument" :disabled="busy || !newName.trim()">添加到所属文档列表</button>
      </div>
      <div v-if="rows.length" class="source-table-wrap">
        <table class="source-table">
          <thead><tr><th><input type="checkbox" aria-label="全选摄入文件" :checked="rows.every(row => row.selected)" :disabled="busy" @change="rows.forEach(row => row.selected = $event.target.checked)" /></th><th>原始文件</th><th>所属文档</th><th>版本号</th><th>设为默认版</th></tr></thead>
          <tbody><tr v-for="row in rows" :key="row.sourceFile" :class="{ unselected: !row.selected }">
            <td><input type="checkbox" v-model="row.selected" :aria-label="`摄入 ${row.sourceFile}`" :disabled="busy" /></td>
            <td class="source-filename">{{ row.displayName || row.sourceFile }} <small>{{ row.status === 'indexed' ? '已入库' : row.status === 'changed' ? '内容已变化' : '新增' }}</small></td>
            <td><select v-model="row.documentKey" :aria-label="`${row.sourceFile} 所属文档`" :disabled="busy || !row.selected" @change="row.setDefault = false">
              <option value="" :disabled="Boolean(row.originalDocumentId)">独立文档（按文件名）</option>
              <option v-for="document in documents" :key="document.key" :value="document.key">{{ document.name }}{{ document.id ? '' : '（新建）' }}</option>
            </select></td>
            <td><input v-if="row.documentKey" v-model="row.versionLabel" :aria-label="`${row.sourceFile} 版本号`" maxlength="100" placeholder="例如：1、0314" :disabled="busy || !row.selected" /><span v-else class="source-help">初始版本（自动）</span></td>
            <td><label><input type="checkbox" v-model="row.setDefault" :aria-label="`${row.sourceFile} 作为默认版`" :disabled="busy || !row.selected || !row.documentKey" @change="chooseDefault(row)" />默认</label></td>
          </tr></tbody>
        </table>
      </div>
      <p v-else>这个目录中没有支持的文档。</p>
      <p v-if="unsupported.length" class="source-warning" role="alert">
        以下 {{ unsupported.length }} 个文件格式不支持，本次未上传或未参与摄入：
        {{ unsupported.map(item => item.sourceFile).join('、') }}
      </p>
      <p class="source-help">相同“所属文档”的文件会成为不同版本。未指定默认版时保留原默认版；新文档使用第一个版本。归属设置随本次摄入成功后一起生效。</p>
    </template>
    <p v-else class="source-help">选择目录或文件上传后，在表中选择所属文档和版本。已有项目新增了文件，也可以读取项目已有目录；默认只勾选新增或变化的文件。</p>
  </section>
</template>

<script setup>
import { readApiResponse } from './apiResponse.js';
import { ref, watch } from 'vue';
import { buildIngestionPlan } from './ingestionDocuments.js';
import { directoryEntries, uploadBatches, createUploadSessionId } from './uploadFiles.js';
const props = defineProps({ projectDir: { type: String, default: '' }, rawDir: { type: String, default: '' }, busy: Boolean, projectId: String, projectName: String, preferredDocumentId: String, intakeMode: { type: String, default: 'upload' } });
const documents = ref([]);
const rows = ref([]);
const unsupported = ref([]);
const uploadSummary = ref(null);
const newName = ref('');
const loaded = ref(false);
const pending = ref(false);
const error = ref('');
const receivedTarget = ref(null);
const progress = ref('');
const uploadQueue = ref(null);
let uploadController = null;
let revision = 0;
function addDocument() {
  const name = newName.value.trim();
  if (!name) return;
  if (documents.value.some(document => document.name === name)) { error.value = '列表中已有这个文档，请直接选择。'; return; }
  documents.value.push({ key: `new:${name}`, name });
  newName.value = '';
  error.value = '';
}
function chooseDefault(row) {
  if (row.setDefault) rows.value.forEach(other => { if (other !== row && other.documentKey === row.documentKey) other.setDefault = false; });
}
async function load() {
  const token = ++revision;
  pending.value = true;
  error.value = '';
  try {
    const response = await fetch('/api/ingestion/sources/', {
      method: 'POST', credentials: 'include', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ mode: props.intakeMode, sourceDir: props.intakeMode === 'server' ? props.rawDir : undefined,
        projectId: props.projectId, projectDir: props.projectDir, rawDir: props.rawDir, projectName: props.projectName })
    });
    const data = await readApiResponse(response);
    if (token !== revision) return;
    apply(data, false);
    loaded.value = true;
  } catch (err) { if (token === revision) { error.value = err.message; loaded.value = false; } }
  finally { if (token === revision) pending.value = false; }
}
function apply(data, append) {
  receivedTarget.value = data.target || { path: props.projectDir, rawDir: props.rawDir, name: props.projectName };
  const known = new Map(documents.value.map(document => [document.key, document]));
  for (const document of data.documents || []) known.set(document.id, { ...document, key: document.id });
  documents.value = [...known.values()];
  unsupported.value = data.unsupported || [];
  const rowsByPath = new Map((append ? rows.value : []).map(row => [row.sourceFile, row]));
  for (const file of data.files || []) {
    if (append && rowsByPath.has(file.sourceFile)) continue;
    rowsByPath.set(file.sourceFile, {
      ...file, selected: file.status !== 'indexed', documentKey: file.logicalDocumentId || (documents.value.some(document => document.key === props.preferredDocumentId) ? props.preferredDocumentId : ''),
      originalDocumentId: file.logicalDocumentId || '', versionLabel: file.versionLabel || '', setDefault: false
    });
  }
  rows.value = [...rowsByPath.values()];
}
async function upload(event) {
  const input = event.target;
  const chosen = [...input.files]; input.value = '';
  if (!chosen.length) return;
  if (uploadQueue.value) { error.value = '请先继续上传剩余文件，或取消上一次未完成的上传'; return; }
  if (!props.projectId && !props.projectName?.trim()) { error.value = '请先填写新项目名称'; return; }
  const allowed = /\.(doc|docx|xlsx|xls|pptx|pdf|md|txt|csv|html|htm)$/i;
  const skipped = chosen.filter(file => !allowed.test(file.name) || file.name.startsWith('~$'));
  uploadSummary.value = { chosen: chosen.length, uploaded: 0, skipped: skipped.length };
  unsupported.value = skipped.map(file => ({ sourceFile: file.webkitRelativePath || file.name }));
  const files = chosen.filter(file => allowed.test(file.name) && !file.name.startsWith('~$'));
  if (files.length > 1000 || files.reduce((total, file) => total + file.size, 0) > 512 * 1024 * 1024) { error.value = '单次最多添加1000份文件，总大小不超过512MB'; return; }
  if (!files.length) { error.value = '所选文件暂不支持，请选择 Word（DOC/DOCX）、Excel（XLSX/XLS）、PowerPoint、PDF、Markdown、TXT、CSV 或 HTML'; return; }
  try {
    uploadQueue.value = { sessionId: createUploadSessionId(), entries: directoryEntries(files), next: 0, skipped: chosen.length - files.length, skippedFiles: unsupported.value };
    await continueUpload();
  } catch (err) { error.value = err.message; }
}
async function continueUpload() {
  const queue = uploadQueue.value;
  if (!queue) return;
  const token = ++revision;
  pending.value = true; error.value = '';
  uploadController = new AbortController();
  progress.value = `正在上传：${queue.next}/${queue.entries.length} 个文件`;
  try {
    await uploadBatches(queue.entries, async batch => {
      const body = new FormData();
      body.append('uploadSessionId', queue.sessionId);
      if (props.projectId) body.append('projectId', props.projectId);
      else {
        body.append('projectName', props.projectName.trim());
        if (receivedTarget.value?.path) body.append('projectDir', receivedTarget.value.path);
      }
      batch.forEach(entry => body.append('files', entry.file));
      body.append('paths', JSON.stringify(batch.map(entry => entry.path)));
      return readApiResponse(await fetch('/api/ingestion/upload/', { method: 'POST', credentials: 'include', body, signal: uploadController.signal }));
    }, {
      start: queue.next, cancelled: () => token !== revision,
      onBatch(data, completed, total) {
        apply(data, true); loaded.value = true; queue.next = completed;
        uploadSummary.value.uploaded = completed;
        unsupported.value = queue.skippedFiles;
        progress.value = `已上传 ${completed}/${total} 个文件，子目录层级已保留`;
      }
    });
    if (token !== revision) return;
    uploadQueue.value = null;
    if (queue.skipped) error.value = `已跳过 ${queue.skipped} 份不支持的文件或临时文件。`;
  } catch (err) { if (token === revision && err.name !== 'AbortError') error.value = err.message; }
  finally { if (token === revision) { pending.value = false; uploadController = null; } }
}
function cancelRemaining() { uploadQueue.value = null; error.value = ''; progress.value = ''; }
function getPlan() {
  if (uploadQueue.value) throw new Error('还有文件未上传完成，请先继续上传或明确取消剩余文件');
  if (!loaded.value || pending.value) throw new Error('请先添加文件或读取项目已有目录');
  const plan = buildIngestionPlan(rows.value, documents.value);
  const target = receivedTarget.value;
  return { ...plan, sourceFiles: rows.value.filter(row => row.selected).map(row => row.sourceFile),
    uploadReceipt: uploadSummary.value ? { ...uploadSummary.value, skippedFiles: unsupported.value.map(item => item.sourceFile) } : undefined,
    projectDir: target.path, rawDir: target.rawDir, exportDir: target.exportDir || '', projectName: props.projectName || target.name, targetProjectId: props.projectId || undefined };
}
watch(() => [props.projectDir, props.rawDir, props.projectId, props.intakeMode], () => {
  revision++;
  uploadController?.abort(); uploadController = null; uploadQueue.value = null; progress.value = '';
  receivedTarget.value = null;
  rows.value = [];
  documents.value = [];
  unsupported.value = [];
  uploadSummary.value = null;
  loaded.value = false;
  pending.value = false;
  error.value = '';
});
defineExpose({ getPlan });
</script>

<style scoped>
.upload-button { position: relative; display: inline-flex; cursor: pointer; border: 1px solid #ccd7e2; border-radius: 7px; padding: 9px 12px; background: white; font-size: 13px; }
.upload-button input { position: absolute; inset: 0; width: 100%; height: 100%; opacity: 0; cursor: pointer; }
.upload-button:focus-within { outline: 2px solid #3b82f6; outline-offset: 2px; }
.upload-button:has(input:disabled) { opacity: .5; cursor: default; }
.source-filename small { display: inline-block; color: #64748b; margin-left: 6px; }
.ingestion-documents { margin: 16px 0 22px; padding: 18px; border: 1px solid #d7e0e8; border-radius: 10px; }
.source-heading, .source-toolbar { display: flex; align-items: center; flex-wrap: wrap; gap: 12px; }
.source-heading { justify-content: space-between; }
.source-heading h3 { margin: 0 0 6px; }
.source-heading p, .source-help { margin: 6px 0; color: #64748b; font-size: 13px; }
.source-toolbar { margin: 16px 0 12px; font-size: 13px; }
.source-toolbar > span { margin-right: auto; }
.source-toolbar input { width: 180px; }
.source-table-wrap { overflow-x: auto; }
.source-table { width: 100%; min-width: 640px; border-collapse: collapse; }
.source-table th, .source-table td { text-align: left; padding: 12px 8px; border-bottom: 1px solid #e2e8f0; }
.source-table th { background: #f5f8fb; font-size: 13px; }
.source-table input:not([type=checkbox]), .source-table select { width: 100%; min-width: 100px; }
.source-table th:first-child { width: 36px; }
.source-table th:last-child { width: 105px; }
.source-table td:nth-child(4) { width: 150px; }
.source-filename { overflow-wrap: anywhere; }
.unselected { opacity: .55; }
.source-error { color: #b42318; }
.source-warning { margin: 12px 0 0; padding: 10px 12px; border: 1px solid #fde68a; border-radius: 8px; background: #fffbeb; color: #b45309; font-size: 13px; line-height: 1.6; }
.source-help { margin-top: 12px; line-height: 1.6; }
</style>
