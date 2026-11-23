<template>
  <section class="version-library">
    <div class="library-heading"><div><h3>已入库文档</h3><p>管理已保存的版本。需要新版时点击“添加新版本”，选择新文件即可；已有版本无需重新摄入。</p></div>
      <button type="button" @click="load" :disabled="busy || pending || !projectId">刷新</button>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <template v-if="needsMigration">
      <p>这个知识库的已有文件尚未整理为文档版本。</p>
      <button type="button" @click="save({ action: 'initialize' })" :disabled="busy || pending">整理已有文档</button>
    </template>
    <div v-else-if="groups.length" class="library-table-wrap"><table class="library-table">
      <thead><tr><th>文档</th><th>已保存版本／原文件</th><th>查询时默认使用</th></tr></thead>
      <tbody><tr v-for="group in groups" :key="group.id">
        <td><strong>{{ group.name }}</strong><button type="button" @click="$emit('add-version', group)">添加新版本</button></td>
        <td><div v-for="version in group.versions" :key="version.editionId" class="library-version">
          <input v-model="labels[version.editionId]" :aria-label="`${group.name} 的版本号`" maxlength="100" style="width:150px" /><span>{{ sourceName(version.sourceRef) }}</span>
          <button type="button" @click="save({editionId: version.editionId, versionLabel: labels[version.editionId]})" :disabled="busy || pending || labels[version.editionId] === version.versionLabel">保存版本号</button>
          <button type="button" @click="$emit('preview', version)" :disabled="busy || pending">查看原文</button>
        </div></td>
        <td><div class="default-controls"><select v-model="defaults[group.id]" :aria-label="`${group.name} 的查询默认版本`" :disabled="busy || pending">
          <option v-for="version in group.versions" :key="version.editionId" :value="version.editionId" :disabled="version.status !== 'ready'">{{ version.versionLabel }}</option>
        </select><button type="button" @click="save({editionId: defaults[group.id], setDefault: true})" :disabled="busy || pending || !defaults[group.id] || group.versions.some(v => v.editionId === defaults[group.id] && v.isDefaultVersion)">保存默认版</button></div></td>
      </tr></tbody>
    </table></div>
    <p v-else>暂无已入库文档。完成摄入后会在这里显示。</p>
    <p v-if="needsReingestion" class="library-note">部分旧数据缺少版本来源；重新摄入后会补齐。</p>
    <p v-if="unavailableHistoryCount" class="library-note">有 {{ unavailableHistoryCount }} 个早期记录缺少完整历史正文，需要原文件才能恢复。</p>
  </section>
</template>

<script setup>
import { readApiResponse } from './apiResponse.js';
import { ref, watch } from 'vue';
const props = defineProps({ projectId: String, refreshKey: Number, busy: Boolean });
const emit = defineEmits(['preview', 'changed', 'add-version']);
const groups = ref([]);
const defaults = ref({});
const labels = ref({});
const needsMigration = ref(false);
const needsReingestion = ref(false);
const unavailableHistoryCount = ref(0);
const pending = ref(false);
const error = ref('');
let revision = 0;
function sourceName(ref) { return ref.startsWith('上传文件/') ? ref.split('/').slice(2).join('/') : ref; }
function apply(data) {
  groups.value = data.documents || [];
  labels.value = Object.fromEntries(groups.value.flatMap(group => group.versions.map(v => [v.editionId, v.versionLabel])));
  defaults.value = Object.fromEntries(groups.value.map(group => [group.id, group.versions.find(v => v.isDefaultVersion)?.editionId || '']));
  needsMigration.value = Boolean(data.needsMigration);
  needsReingestion.value = Boolean(data.needsReingestion);
  unavailableHistoryCount.value = data.unavailableHistoryCount || 0;
}
async function request(body) {
  const response = await fetch(`/api/projects/${encodeURIComponent(props.projectId)}/document-versions/`, {
    credentials: 'include', ...(body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : {})
  });
  const data = await readApiResponse(response);
  return data;
}
async function load() {
  const token = ++revision;
  if (!props.projectId) { apply({}); pending.value = false; return; }
  pending.value = true;
  error.value = '';
  try { const data = await request(); if (token === revision) apply(data); }
  catch (err) { if (token === revision) error.value = err.message; }
  finally { if (token === revision) pending.value = false; }
}
async function save(body) {
  const token = ++revision;
  pending.value = true;
  error.value = '';
  try { const data = await request(body); if (token === revision) { apply(data); emit('changed'); } }
  catch (err) { if (token === revision) error.value = err.message; }
  finally { if (token === revision) pending.value = false; }
}
watch(() => [props.projectId, props.refreshKey], load, { immediate: true });
</script>

<style scoped>
.version-library { border-top: 1px solid #dce4ec; margin: 24px 0; padding-top: 20px; }
.library-heading { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.library-heading h3 { margin: 0 0 6px; }
.library-heading p, .library-note { color: #64748b; font-size: 13px; }
.library-table-wrap { overflow-x: auto; }
.library-table { width: 100%; min-width: 600px; border-collapse: collapse; }
.library-table th, .library-table td { text-align: left; padding: 12px; border-bottom: 1px solid #e2e8f0; vertical-align: top; }
.library-table th { background: #f5f8fb; font-size: 13px; }
.library-version { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; margin-bottom: 8px; }
.library-version span { color: #64748b; font-size: 12px; overflow-wrap: anywhere; }
.library-version button { font-size: 12px; padding: 4px 8px; }
.default-controls { display: flex; align-items: center; flex-wrap: wrap; gap: 8px; }
</style>
