<template>
  <section class="panel">
    <div class="panel-head"><div><h2>已登记项目</h2><p>管理项目登记和显示名称。添加知识文件请进入“知识摄入”。</p></div>
      <div class="actions"><button @click="showRegister = !showRegister">登记已有项目</button><button @click="$emit('refresh')">刷新</button></div>
    </div>
    <p v-if="error" role="alert">{{ error }}</p>
    <form v-if="showRegister" class="registry-form" @submit.prevent="register">
      <label class="field"><span>已有项目目录（服务端）</span><input v-model="form.projectDir" required placeholder="选择已经做好知识库的目录，例如 D:\知识库\交易规则" /></label>
      <label class="field"><span>显示名称</span><input v-model="form.projectName" maxlength="200" placeholder="留空使用目录名称" /></label>
      <label class="field"><span>原文目录（可选）</span><input v-model="form.rawDir" placeholder="留空自动识别项目下的原文目录" /></label>
      <div class="actions"><button class="primary" :disabled="pending">登记</button><button type="button" @click="showRegister = false">取消</button></div>
    </form>
    <p v-if="!projects.length">还没有登记项目。可登记已有目录，也可在知识摄入时新建项目。</p>
    <article v-for="project in projects" :key="project.id" class="project-row">
      <div><input v-if="editing === project.id" v-model="name" maxlength="200" :aria-label="`${project.name} 的显示名称`" /><strong v-else>{{ project.name }}</strong><p>{{ project.path }}</p></div>
      <div class="actions compact" v-if="editing === project.id"><button @click="rename(project)" :disabled="pending || !name.trim()">保存名称</button><button @click="editing = ''">取消</button></div>
      <div class="actions compact" v-else><button @click="$emit('select', project)">选择</button><button @click="editing = project.id; name = project.name">改名</button><button @click="remove(project)" :disabled="pending">移除登记</button></div>
    </article>
    <p class="registry-help">移除登记会保留项目目录中的原文与历史文件。</p>
  </section>
</template>
<script setup>
import { ref } from 'vue';
import { readApiResponse } from './apiResponse.js';
defineProps({ projects: { type: Array, default: () => [] } });
const emit = defineEmits(['refresh', 'select']);
const showRegister = ref(false), editing = ref(''), name = ref(''), error = ref(''), pending = ref(false);
const form = ref({ projectDir: '', projectName: '', rawDir: '' });
async function request(url, method, body) {
  pending.value = true; error.value = '';
  try {
    const response = await fetch(url, { method, credentials: 'include', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    const data = await readApiResponse(response); emit('refresh'); return data;
  } catch (err) { error.value = err.message; }
  finally { pending.value = false; }
}
async function register() { const data = await request('/api/projects/register/', 'POST', form.value); if (data) { showRegister.value = false; emit('select', data.registeredProject); } }
async function rename(project) { if (await request('/api/projects/', 'PATCH', { id: project.id, name: name.value })) editing.value = ''; }
async function remove(project) { await request('/api/projects/', 'DELETE', { id: project.id }); }
</script>
<style scoped>
.registry-form { padding: 16px; margin: 16px 0; border: 1px solid #d7e0e8; border-radius: 8px; display: grid; gap: 12px; }
.registry-help { color: #64748b; font-size: 13px; margin-top: 18px; }
</style>
