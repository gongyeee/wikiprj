<template>
  <div class="markdown-answer">
    <template v-for="(block, blockIndex) in blocks" :key="blockIndex">
      <h3 v-if="block.type === 'heading'">
        <template v-for="(part, partIndex) in block.parts" :key="partIndex">
          <strong v-if="part.type === 'strong'">{{ part.text }}</strong>
          <code v-else-if="part.type === 'code'">{{ part.text }}</code>
          <span v-else>{{ part.text }}</span>
        </template>
      </h3>
      <ul v-else-if="block.type === 'list'">
        <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
          <template v-for="(part, partIndex) in item.parts" :key="partIndex">
            <strong v-if="part.type === 'strong'">{{ part.text }}</strong>
            <code v-else-if="part.type === 'code'">{{ part.text }}</code>
            <span v-else>{{ part.text }}</span>
          </template>
        </li>
      </ul>
      <pre v-else-if="block.type === 'codeblock'" class="markdown-codeblock"><code>{{ block.text }}</code></pre>
      <table v-else-if="block.type === 'table'" class="markdown-table">
        <thead>
          <tr>
            <th v-for="(cell, cellIndex) in block.headers" :key="cellIndex">{{ cell }}</th>
          </tr>
        </thead>
        <tbody>
          <tr v-for="(row, rowIndex) in block.rows" :key="rowIndex">
            <td v-for="(cell, cellIndex) in row" :key="cellIndex">{{ cell }}</td>
          </tr>
        </tbody>
      </table>
      <blockquote v-else-if="block.type === 'quote'">
        <template v-for="(part, partIndex) in block.parts" :key="partIndex">
          <span>{{ part.text }}</span>
        </template>
      </blockquote>
      <p v-else>
        <template v-for="(part, partIndex) in block.parts" :key="partIndex">
          <strong v-if="part.type === 'strong'">{{ part.text }}</strong>
          <code v-else-if="part.type === 'code'">{{ part.text }}</code>
          <span v-else>{{ part.text }}</span>
        </template>
      </p>
    </template>
  </div>
</template>

<script setup>
defineProps({
  blocks: {
    type: Array,
    default: () => []
  }
});
</script>
