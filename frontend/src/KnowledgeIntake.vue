<template>
  <div class="knowledge-intake">
    <div class="intake-target">
      <label class="field"><span>导入到哪个项目</span><select v-model="targetId" :disabled="busy"><option value="">新建项目</option><option v-for="project in projects" :key="project.id" :value="project.id">{{ project.name }}</option></select></label>
      <label v-if="!targetId" class="field"><span>新项目名称</span><input v-model="newName" maxlength="200" placeholder="例如：交易规则" :disabled="busy" /></label>
    </div>
    <div class="intake-method"><label><input type="radio" value="upload" v-model="method" :disabled="busy" /> 上传本机文件／目录</label><label><input type="radio" value="server" v-model="method" :disabled="busy" /> 使用服务端本地目录</label></div>
    <label v-if="method === 'server'" class="field"><span>文档目录（运行服务的电脑可访问的路径）</span><input v-model="serverPath" :disabled="busy" placeholder="例如 D:\文档\交易规则；包含子目录时会递归读取" /></label>
    <p v-else class="intake-help">从当前电脑选择文件，浏览器上传文件内容；无需填写本机或旧服务器路径。</p>
    <IngestionDocuments ref="files" :intake-mode="method" :project-id="targetId" :project-dir="target?.path || ''" :raw-dir="method === 'server' ? serverPath : target?.rawDir || ''" :project-name="target?.name || newName" :preferred-document-id="preferredDocumentId" :busy="busy" />
  </div>
</template>
<script setup>
import { computed, ref, watch } from 'vue';
import IngestionDocuments from './IngestionDocuments.vue';
const props = defineProps({ projects: { type: Array, default: () => [] }, projectId: String, preferredDocumentId: String, busy: Boolean });
const targetId = ref(props.projectId || ''), newName = ref(''), files = ref(null);
const method = ref('upload'), serverPath = ref('');
const target = computed(() => props.projects.find(project => project.id === targetId.value));
watch(targetId, () => { serverPath.value = target.value?.rawDir || ''; }, { immediate: true });
watch(() => props.projectId, value => { targetId.value = value || ''; });
defineExpose({ getPlan() { return files.value.getPlan(); } });
</script>
<style scoped>
.intake-method { display:flex; flex-wrap:wrap; gap:20px; margin:16px 0; }
.intake-method label { display:flex; align-items:center; gap:8px; white-space:nowrap; }
.intake-method input[type="radio"] { width:auto; flex:none; margin:0; }
.intake-help { color:#64748b; font-size:13px; }
.intake-target { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:16px; }
</style>
