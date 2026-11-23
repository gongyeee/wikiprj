<template>
  <section class="panel prompt-manager">
    <div class="panel-head"><div><h2>提示词管理</h2><p>编辑知识摄入的总结与结构化抽取提示词。保存后用于新任务；正在运行和重试的任务沿用原快照。</p></div></div>
    <p>下方两种正文模板必须保留 <code>${section_text}</code>（原文）和 <code>${output_schema}</code>（返回结构），还可用 <code>${source_name}</code>、<code>${heading_path}</code>。普通美元符号写成 <code>$$</code>。系统提示词为纯文本，不展开变量。</p>
    <p v-if="error" role="alert">{{ error }}</p>
    <p v-if="message" role="status">{{ message }}</p>
    <template v-if="prompts">
      <label v-for="field in fields" :key="field.key" class="field"><span>{{ field.label }}</span><textarea v-model="prompts[field.key]" :rows="field.key === 'system' ? 4 : 18" :disabled="pending"></textarea></label>
      <div class="actions"><button class="primary" @click="save" :disabled="pending">保存提示词</button><button @click="restore" :disabled="pending">恢复默认到编辑框</button></div>
    </template>
  </section>
</template>
<script setup>
import { onMounted, ref } from 'vue';
import { readApiResponse } from './apiResponse.js';
const fields = [{ key: 'system', label: '抽取系统提示词' }, { key: 'compact', label: '精简总结与抽取' }, { key: 'standard', label: '标准总结与抽取' }];
const prompts = ref(null), defaults = ref(null), pending = ref(false), error = ref(''), message = ref('');
async function request(method) {
  pending.value = true; error.value = ''; message.value = '';
  try {
    const response = await fetch('/api/ingestion/prompts/', { method, credentials: 'include', headers: { 'Content-Type': 'application/json' }, ...(method === 'POST' ? { body: JSON.stringify({ prompts: prompts.value }) } : {}) });
    const data = await readApiResponse(response);
    prompts.value = { ...data.prompts }; defaults.value = { ...data.defaults };
    if (method === 'POST') message.value = '已保存。新任务将使用这些提示词。';
  } catch (err) { error.value = err.message; }
  finally { pending.value = false; }
}
function save() { return request('POST'); }
function restore() { prompts.value = { ...defaults.value }; message.value = '已恢复到编辑框，点击保存后生效。'; }
onMounted(() => request('GET'));
</script>
<style scoped>
.prompt-manager .field { margin: 18px 0; }
textarea { width: 100%; box-sizing: border-box; resize: vertical; font-family: monospace; line-height: 1.6; }
p { line-height: 1.6; }
</style>
