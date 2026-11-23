<template>
  <div class="query-versions">
    <strong>查询版本</strong>
    <template v-if="groups.some(group => group.versions.length > 1)">
      <label>文档 <select v-model="documentId" :disabled="busy"><option value="">使用各文档默认版</option><option v-for="group in groups.filter(g => g.versions.length > 1)" :key="group.id" :value="group.id">{{ group.name }}</option></select></label>
      <label v-if="activeDocument">版本 <select :value="modelValue[documentId] || ''" @change="select(documentId, $event.target.value)" :disabled="busy"><option value="">默认版</option><option v-for="version in activeDocument.versions" :key="version.editionId" :value="version.editionId">{{ version.versionLabel }}{{ version.isDefaultVersion ? '（默认）' : '' }}</option></select></label>
    </template>
    <span v-else>使用各文档默认版</span>
    <span v-for="[id, editionId] in Object.entries(modelValue)" :key="id" class="version-chip">{{ name(id, editionId) }} <button type="button" @click="select(id, '')" :disabled="busy">恢复默认</button></span>
    <span v-if="error" role="alert">{{ error }}</span>
  </div>
</template>
<script setup>
import { computed, ref, watch } from 'vue';
import { readApiResponse } from './apiResponse.js';
const props = defineProps({ projectId: String, refreshKey: Number, busy: Boolean, modelValue: { type: Object, default: () => ({}) } });
const emit = defineEmits(['update:modelValue']);
const groups = ref([]), documentId = ref(''), error = ref('');
const activeDocument = computed(() => groups.value.find(group => group.id === documentId.value));
let revision = 0;
function select(id, value) { const scope = { ...props.modelValue }; if (value) scope[id] = value; else delete scope[id]; emit('update:modelValue', scope); }
function name(id, editionId) { const group = groups.value.find(g => g.id === id); return `${group?.name || '文档'} · ${group?.versions.find(v => v.editionId === editionId)?.versionLabel || '版本已失效'}`; }
watch(() => [props.projectId, props.refreshKey], async () => {
  const token = ++revision; groups.value = []; error.value = ''; documentId.value = '';
  if (!props.projectId) return;
  try { const data = await readApiResponse(await fetch(`/api/projects/${encodeURIComponent(props.projectId)}/document-versions/`, { credentials: 'include' })); if (token === revision) groups.value = data.documents || []; }
  catch (err) { if (token === revision) error.value = err.message; }
}, { immediate: true });
</script>
<style scoped>
.query-versions { display:flex; align-items:center; flex-wrap:wrap; gap:10px; font-size:13px; }
.query-versions label { display:flex; align-items:center; gap:6px; white-space:nowrap; max-width:100%; }
.query-versions select { width:auto; min-width:0; max-width:260px; }
.version-chip { background:#eef4fa; padding:5px 8px; border-radius:6px; }
</style>
