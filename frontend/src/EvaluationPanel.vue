<template>
  <section class="eval-panel">
    <header class="eval-heading">
      <div><div class="eval-eyebrow">管理员 · 质量评测</div><h2>问答评测</h2><p>维护标准问答，运行实际问答流程，逐题核对答案与评分。</p></div>
      <button @click="refresh" :disabled="busy">刷新</button>
    </header>
    <p v-if="!projectId" class="eval-notice">请先在顶部选择项目。</p>
    <template v-else>
      <p class="eval-notice">当前项目：{{ projects.find(p => p.id === projectId)?.name || projectId }} · 项目 ID：<code>{{ projectId }}</code></p>
      <nav class="eval-tabs" aria-label="评测模块">
        <button :class="{active: tab === 'cases'}" @click="tab = 'cases'">标准问答库 <span>{{ total }}</span></button>
        <button :class="{active: tab === 'runs'}" @click="tab = 'runs'; loadRuns()">评测任务</button>
        <button :class="{active: tab === 'report'}" :disabled="!report" @click="tab = 'report'">答案审查报告</button>
      </nav>
      <p v-if="message" class="eval-notice" role="status">{{ message }}</p>
      <p v-if="error" class="eval-error" role="alert">{{ error }}</p>

      <template v-if="tab === 'cases'">
        <form class="eval-filters" @submit.prevent="applyFilters">
          <input v-model="filters.q" placeholder="搜索问题、答案或关键词" aria-label="搜索问答" />
          <select v-model="filters.category" aria-label="分类"><option value="">全部分类</option><option v-for="c in categories" :key="c">{{ c }}</option></select>
          <select v-model="filters.scene" aria-label="场景"><option value="">全部场景</option><option v-for="s in scenes" :key="s">{{ s }}</option></select>
          <select v-model="filters.status" aria-label="状态"><option value="">全部状态</option><option value="enabled">启用</option><option value="disabled">停用</option><option value="pending">待核对</option></select>
          <select v-model="filters.kind" aria-label="用例类型"><option value="">全部类型</option><option value="answer">普通问答</option><option value="refusal">应拒答</option></select>
          <button :disabled="busy">查询</button>
        </form>
        <div class="eval-actions">
          <button @click="editCase()">新增问答</button><button @click="openOperation('paste')">批量粘贴</button>
          <button @click="openOperation('import')">Excel 导入</button><button @click="download('/template/', '问答导入模板.xlsx')">下载模板</button>
          <button class="primary" @click="openOperation('evaluate')">一键评测</button>
        </div>
        <div class="eval-selection">
          <span>已选 {{ selectedCount }} 条</span><button @click="selectPage">选择本页</button><button @click="selectAll">选择全部筛选结果（{{ total }}）</button><button @click="clearSelection">清空</button>
          <button :disabled="!selectedCount" @click="openOperation('bulk')">批量修改</button>
          <button :disabled="!selectedCount" @click="openOperation('transfer')">移动 / 复制</button>
          <button :disabled="!selectedCount" @click="openOperation('delete')">删除</button>
        </div>

        <section v-if="operation" class="eval-operation">
          <header><h3>{{ operationTitles[operation] }}</h3><button @click="closeOperation" :disabled="busy">收起</button></header>
          <form v-if="operation === 'edit'" @submit.prevent="saveCase" class="eval-form">
            <label v-for="key in ['question', 'expected', 'rubric']" :key="key" class="wide">{{ fields[key] }}<textarea v-model="editor[key]" :rows="key === 'expected' ? 8 : 3" :required="key === 'question' || key === 'expected' && editor.kind === 'answer'"></textarea></label>
            <label v-for="key in ['category', 'scene', 'keywords']" :key="key">{{ fields[key] }}<input v-model="editor[key]" /></label>
            <label>用例类型<select v-model="editor.kind"><option value="answer">普通问答</option><option value="refusal">应拒答</option></select></label>
            <label>状态<select v-model="editor.status"><option value="enabled">启用</option><option value="disabled">停用</option><option value="pending">待核对</option></select></label>
            <label>引用文档名称（每行一个）<textarea v-model="editor.reference_names" rows="3"></textarea></label>
            <label>引用文档路径（每行一个）<textarea v-model="editor.reference_paths" rows="3"></textarea></label>
            <div class="wide"><button class="primary" :disabled="busy">保存问答</button></div>
          </form>
          <template v-if="operation === 'paste'">
            <p>从 Excel 复制后粘贴，列顺序：问题、预期答案、分类、场景、用例类型、引用文档名称、引用文档路径。前两列必填（拒答类可省略预期答案）。支持单元格内换行。</p>
            <textarea v-model="pasteText" rows="9" aria-label="批量粘贴问答" placeholder="问题&#9;预期答案&#9;分类&#9;场景"></textarea>
            <label>同项目重复问题 <select v-model="conflict"><option value="skip">跳过</option><option value="update">更新已有记录</option></select></label>
            <button @click="previewPaste" :disabled="busy">校验并预览</button>
          </template>
          <template v-if="operation === 'import'">
            <p>选择工作表和字段映射后预览。最多 8 MB、2000 条；不会执行 Excel 公式。所属项目列留空时导入当前项目。</p>
            <div class="eval-actions"><input type="file" accept=".xlsx" @change="chooseFile" aria-label="Excel 文件" :disabled="busy" />
              <label>工作表 <select v-model="sheet" @change="inspectSheet" :disabled="busy"><option value="">请选择</option><option v-for="s in sheets" :key="s">{{ s }}</option></select></label>
              <label>表头行 <input type="number" v-model.number="headerRow" min="1" max="100" @change="inspectSheet" :disabled="busy" /></label>
            </div>
            <div v-if="headers.length" class="eval-mapping">
              <label v-for="(label, key) in {project:'所属项目', ...fields}" :key="key">{{ label }}<select v-model="mapping[key]"><option value="">不导入此列</option><option v-for="h in headers" :key="h.column" :value="h.column">{{ h.column }} · {{ h.label || '空表头' }}</option></select></label>
            </div>
            <label>同项目重复问题 <select v-model="conflict"><option value="skip">跳过</option><option value="update">更新已有记录</option></select></label>
            <button @click="previewExcel" :disabled="busy || !sheet || !mapping.question">校验并预览</button>
          </template>
          <template v-if="operation === 'bulk'">
            <div class="eval-actions"><select v-model="bulkField" aria-label="修改字段"><option value="status">状态</option><option value="category">分类</option><option value="scene">场景</option></select>
              <select v-if="bulkField === 'status'" v-model="bulkStatus"><option value="enabled">启用</option><option value="disabled">停用</option><option value="pending">待核对</option></select><input v-else v-model="bulkValue" placeholder="新值（可留空）" />
              <button @click="updateBulk" :disabled="busy">应用到 {{ selectedCount }} 条</button></div>
          </template>
          <template v-if="operation === 'delete'"><p>将删除当前项目中所选 {{ selectedCount }} 条标准问答。已有评测报告仍保留当时的完整快照。</p><button class="eval-danger" @click="deleteBulk" :disabled="busy">确认删除 {{ selectedCount }} 条</button></template>
          <template v-if="operation === 'transfer'">
            <p>移动会从源项目移除；复制会保留原记录。同题冲突跳过，引用在目标项目重新核对，未匹配的记录标记待核对。</p>
            <div class="eval-actions"><select v-model="transferAction" aria-label="迁移方式"><option value="move">移动</option><option value="copy">复制</option></select>
              <select v-model="targetProject" aria-label="目标项目"><option value="">选择目标项目</option><option v-for="p in projects.filter(p => p.id !== projectId)" :key="p.id" :value="p.id">{{ p.name || p.title }}</option></select>
              <button @click="previewTransfer" :disabled="busy || !targetProject">预览 {{ selectedCount }} 条</button></div>
          </template>
          <template v-if="operation === 'evaluate'">
            <p>评测范围：{{ selectedCount ? `已选 ${selectedCount} 条` : `当前筛选结果 ${total} 条` }}，仅运行其中已启用的问答。每项目同时运行一个任务。</p>
            <div class="eval-form">
              <label>回答模型<select v-model="settings.modelId"><option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}</option></select><small>{{ modelService(settings.modelId) }}</small></label>
              <label>评分模型<select v-model="settings.judgeModelId"><option v-for="m in models" :key="m.id" :value="m.id">{{ m.name }}</option></select><small>{{ modelService(settings.judgeModelId) }}</small></label>
              <label>检索条数 K<input type="number" v-model.number="settings.limit" min="1" max="50" /></label>
              <label>通过分数<input type="number" v-model.number="settings.threshold" min="0" max="100" /></label>
              <label>上下文字数预算<input type="number" v-model.number="settings.contextMaxChars" min="1000" max="200000" /></label>
              <label>每段证据预留字数<input type="number" v-model.number="settings.contextReserveChars" min="0" max="200000" /></label>
              <label class="eval-check"><input type="checkbox" v-model="settings.useIntent" />使用意图分析</label><label class="eval-check"><input type="checkbox" v-model="settings.useSynonyms" />使用同义词</label>
            </div>
            <p class="eval-muted">普通问答：正确性 50 + 完整性 30 + 证据支持 20；关键事实错误不通过。应拒答类按是否正确拒答评分。自动分需要人工抽查。</p>
            <button class="primary" @click="startRun" :disabled="busy || !total">开始评测</button>
          </template>

          <div v-if="preview" class="eval-preview">
            <h4>本次操作预览 <span v-if="preview.target_name">→ {{ preview.target_name }}</span></h4>
            <p>{{ countsText(preview.counts) }}</p>
            <p>错误行不会写入；引用警告不阻止导入。下面展示前 30 行，完整结果可下载查看。</p>
            <div class="eval-table-wrap"><table><thead><tr><th>工作表 / 行号</th><th>问题 / 项目</th><th>操作</th><th>校验结果</th></tr></thead><tbody><tr v-for="(r, i) in preview.rows.slice(0, 30)" :key="i"><td>{{ r.sheet || '迁移' }} / {{ r.row || i + 1 }}</td><td>{{ r.data?.question }}<small>{{ r.project_name }}</small></td><td>{{ label(r.action) }}</td><td>{{ r.error || (r.data?.reference_check?.verified ? '引用已匹配' : '引用未完全匹配，需核对') }}</td></tr></tbody></table></div>
            <div class="eval-actions"><button @click="download(`/drafts/${preview.draft_id}/report/`, '批量处理明细.xlsx')">下载完整明细</button><button class="primary" @click="commitPreview" :disabled="busy || !preview.rows.some(r => !['skip','error'].includes(r.action))">确认执行上述预览</button></div>
          </div>
        </section>

        <div class="eval-table-wrap"><table><thead><tr><th>选择</th><th>问题</th><th>分类 / 场景</th><th>类型 / 状态</th><th>引用</th><th>维护信息</th><th>操作</th></tr></thead><tbody>
          <tr v-for="item in items" :key="item.id"><td><input type="checkbox" :checked="allFiltered || selected.includes(item.id)" @change="toggleCase(item.id)" :aria-label="`选择 ${item.question}`" /></td><td class="eval-question">{{ item.question }}</td><td>{{ item.category || '—' }}<small>{{ item.scene || '—' }}</small></td><td>{{ label(item.kind) }}<small>{{ item.status === 'pending' ? '待核对' : label(item.status) }}</small></td><td>{{ item.reference_check?.verified ? '已匹配' : '待核验' }}</td><td>{{ item.updated_by }}<small>{{ date(item.updated_at) }}</small></td><td><button @click="editCase(item)">查看 / 编辑</button></td></tr>
          <tr v-if="!items.length"><td colspan="7" class="eval-empty">暂无问答，可从 Excel 导入或手工添加。</td></tr>
        </tbody></table></div>
        <div class="eval-pager"><button :disabled="page <= 1 || busy" @click="page--; loadCases()">上一页</button><span>第 {{ page }} 页 · 共 {{ total }} 条</span><button :disabled="page * 20 >= total || busy" @click="page++; loadCases()">下一页</button></div>
      </template>

      <template v-if="tab === 'runs'">
        <p class="eval-muted">任务在后台运行，关闭页面不会停止。失败项重试会创建新批次，保留原结果和复核记录。</p>
        <article v-for="run in runs" :key="run.id" class="eval-run">
          <div><strong>{{ date(run.created_at) }}</strong><span class="eval-badge">{{ label(run.status) }}{{ run.cancel_requested && isActive(run) ? ' · 正在取消' : '' }}</span><small>{{ run.id }} · {{ run.created_by }}</small></div>
          <p>{{ run.summary.scored }} / {{ run.summary.total }} 已评分 · {{ run.summary.failed }} 失败 · {{ run.summary.unfinished }} 未完成 · 平均 {{ run.summary.average ?? '—' }} 分 · 已复核 {{ run.summary.reviewed }} 条</p>
          <p v-if="run.error" class="eval-error">{{ run.error }}</p>
          <div class="eval-actions"><button class="primary" @click="openReport(run.id)">查看答案与审查</button><button @click="download(`/runs/${run.id}/report/`, '问答评测审查报告.xlsx')">导出报告</button><button v-if="isActive(run)" :disabled="busy" @click="cancelRun(run.id)">取消任务</button><button v-else :disabled="busy || !run.summary.retryable" @click="retryRun(run.id)">重试失败 / 未完成 / 需重跑项</button></div>
        </article>
        <p v-if="!runs.length" class="eval-empty">尚无评测任务。在标准问答库选择评测范围后开始。</p>
        <div class="eval-pager"><button :disabled="runPage <= 1" @click="runPage--; loadRuns()">上一页</button><span>第 {{ runPage }} 页 · 共 {{ runTotal }} 个任务</span><button :disabled="runPage * 10 >= runTotal" @click="runPage++; loadRuns()">下一页</button></div>
      </template>

      <template v-if="tab === 'report' && report">
        <div class="eval-actions"><strong>{{ report.project_name }} · {{ label(report.status) }}</strong><button @click="download(`/runs/${report.id}/report/`, '问答评测审查报告.xlsx')">导出完整审查报告</button><span class="eval-muted">{{ date(report.created_at) }}</span></div>
        <p v-if="report.summary.index_changed" class="eval-error">本批次运行期间知识库发生变化，不建议用于直接比较。</p>
        <p v-if="report.error" class="eval-error">{{ report.error }}</p>
        <div class="eval-metrics"><div><strong>{{ report.summary.average ?? '—' }}</strong><span>自动平均分 · 已评分 {{ report.summary.scored }}</span></div><div><strong>{{ percent(report.summary.pass_rate) }}</strong><span>自动通过率 · 分母为全部 {{ report.summary.total }} 题</span></div><div><strong>{{ report.summary.human_average ?? '—' }}</strong><span>人工确认平均分 · 已复核 {{ report.summary.reviewed }}</span></div><div><strong>{{ percent(report.summary.review_coverage) }}</strong><span>复核覆盖率</span></div></div>
        <p class="eval-muted">失败 {{ report.summary.failed }} 题 · 未完成 {{ report.summary.unfinished }} 题 · 文档 Recall@{{ report.config.limit }}：{{ report.summary.recall_at_k === null ? '暂无完整标注' : percent(report.summary.recall_at_k) }}（有效标注 {{ report.summary.recall_sample_count }} 题） · 拒答正确率 {{ report.summary.refusal_accuracy == null ? "暂无拒答用例" : percent(report.summary.refusal_accuracy) }} · 人工通过率 {{ report.summary.human_pass_rate == null ? "尚未复核" : percent(report.summary.human_pass_rate) }}</p>
        <details><summary>评测配置与分类统计</summary><p>回答：{{ report.config.modelIdSnapshot?.name }}（{{ report.config.modelIdSnapshot?.apiBase }}） / 评分：{{ report.config.judgeModelIdSnapshot?.name }}（{{ report.config.judgeModelIdSnapshot?.apiBase }}）</p><p>通过阈值 {{ report.config.threshold }} · 上下文 {{ report.config.contextMaxChars }} 字 · {{ report.config.evidence_policy }} · 评分规则 {{ report.config.rule_version }}</p><div class="eval-group-stats" v-for="(groups, field) in report.summary.groups" :key="field"><b>{{ fields[field] }}</b><span v-for="g in groups" :key="g.name">{{ label(g.name) }}：{{ g.average ?? '—' }} 分（{{ g.scored }}/{{ g.total }}）</span></div></details>
        <form class="eval-filters" @submit.prevent="resultPage = 1; loadReport()"><input v-model="resultFilters.q" placeholder="按问题搜索" /><select v-model="resultFilters.status"><option value="">全部执行状态</option><option v-for="s in ['scored','answer_failed','score_failed','cancelled','interrupted','pending','running']" :key="s" :value="s">{{ label(s) }}</option></select><select v-model="resultFilters.review"><option value="">全部审查状态</option><option value="pending">尚未完成人工复核</option></select><button>筛选</button></form>
        <div class="eval-review-layout">
          <aside class="eval-result-list"><button v-for="r in results" :key="r.id" :class="{active: resultId === r.id}" @click="chooseResult(r)"><strong>{{ r.snapshot.question }}</strong><small>{{ label(r.status) }} · {{ r.score.total ?? '—' }} 分 · {{ reviewLabel(r.review.status) }}</small></button><p v-if="!results.length">没有符合条件的结果</p></aside>
          <section v-if="currentResult" class="eval-result-detail">
            <h3>{{ currentResult.snapshot.question }}</h3>
            <p class="eval-notice">执行状态：{{ label(currentResult.status) }}<template v-if="currentResult.status === 'running'"> · {{ {retrieval:'正在检索与准备证据', answer:'正在生成实际回答', score:'实际回答已生成，正在评分'}[currentResult.metrics.stage] || '后台处理中' }}</template>。人工审查状态：{{ reviewLabel(currentResult.review.status) }}（需人工提交复核后更新）。</p>
            <p class="eval-muted">{{ currentResult.snapshot.category || '未分类' }} / {{ currentResult.snapshot.scene || '未设置场景' }} · {{ label(currentResult.snapshot.kind) }} · 标准答案版本 {{ currentResult.snapshot.case_revision }}</p>
            <div class="eval-answer-pair"><section><h4>预期回答</h4><div class="eval-fulltext">{{ currentResult.snapshot.expected || '按用例要求正确拒答' }}</div></section><section><h4>实际回答</h4><div class="eval-fulltext">{{ currentResult.answer || '暂无实际回答' }}</div></section></div>
            <p v-if="currentResult.error" class="eval-error">{{ currentResult.error }}</p>
            <section class="eval-score"><h4>自动评分 <strong>{{ currentResult.score.total ?? '—' }} / 100</strong> <span v-if="currentResult.status === 'scored'">{{ currentResult.score.passed ? '通过' : '不通过' }}</span></h4>
              <p v-if="currentResult.snapshot.kind === 'answer'">正确性 {{ currentResult.score.correctness ?? '—' }}/50 · 完整性 {{ currentResult.score.completeness ?? '—' }}/30 · 证据支持 {{ currentResult.score.support ?? '—' }}/20</p>
              <p v-if="currentResult.score.critical_error" class="eval-error">存在关键事实错误</p><div class="eval-fulltext">{{ currentResult.score.reason || '尚无有效自动评分' }}</div>
              <p v-if="currentResult.score.missing_points?.length">遗漏：{{ currentResult.score.missing_points.join('；') }}</p><p v-if="currentResult.score.wrong_points?.length">错误：{{ currentResult.score.wrong_points.join('；') }}</p>
              <p>{{ currentResult.score.reference_status || '标准引用未核验' }} · 检索 {{ currentResult.metrics.search_ms ?? '—' }} ms · 回答 {{ currentResult.metrics.answer_ms ?? '—' }} ms · 总计 {{ currentResult.metrics.total_ms ?? '—' }} ms</p>
            </section>
            <details><summary>标准引用、评分要点与实际证据全文</summary><h4>标准引用</h4><div class="eval-fulltext">{{ currentResult.snapshot.reference_names }}&#10;{{ currentResult.snapshot.reference_paths }}</div><p>匹配：{{ currentResult.snapshot.reference_check?.matched?.join('；') || '无' }}</p><p>未匹配：{{ currentResult.snapshot.reference_check?.unresolved?.join('；') || '无' }}</p><h4>评分要点</h4><div class="eval-fulltext">{{ currentResult.snapshot.rubric || '未单独设置' }}</div><h4>实际送入回答模型的证据</h4><div class="eval-fulltext">{{ currentResult.evidence.context || '无证据' }}</div></details>
            <form class="eval-human" @submit.prevent="saveReview"><h4>人工复核</h4><p>对照两份完整答案，再判断自动评分是否合适。自动评分始终保留。</p><div class="eval-form">
              <label>审查结论<select v-model="reviewForm.status"><option value="pending">待审查</option><option value="accepted" :disabled="currentResult.status !== 'scored'">评分合理</option><option value="adjusted">调整人工评分</option><option value="rerun">需重跑</option></select></label>
              <label v-if="reviewForm.status === 'adjusted'">人工评分<input type="number" v-model.number="reviewForm.score" min="0" max="100" step="0.1" required /></label>
              <label v-if="reviewForm.status === 'adjusted'" class="eval-check"><input type="checkbox" v-model="reviewForm.passed" />人工判定通过</label>
              <label class="wide">审查意见<textarea v-model="reviewForm.comment" rows="4" :required="['adjusted','rerun'].includes(reviewForm.status)" maxlength="10000"></textarea></label>
            </div><button class="primary" :disabled="busy || ['pending','running'].includes(currentResult.status)">保存人工复核</button><span v-if="currentResult.review.by"> {{ currentResult.review.by }} · {{ date(currentResult.review.at) }}</span></form>
            <details v-if="currentResult.review.history?.length"><summary>复核历史（{{ currentResult.review.history.length }}）</summary><div v-for="(entry, i) in currentResult.review.history" :key="i" class="eval-history"><strong>{{ reviewLabel(entry.status) }} · {{ entry.score ?? '—' }} 分</strong><small>{{ entry.by }} · {{ date(entry.at) }}</small><div class="eval-fulltext">{{ entry.comment }}</div></div></details>
          </section>
        </div>
        <div class="eval-pager"><button :disabled="resultPage <= 1" @click="resultPage--; loadReport()">上一页</button><span>第 {{ resultPage }} 页 · 共 {{ resultTotal }} 条结果</span><button :disabled="resultPage * 20 >= resultTotal" @click="resultPage++; loadReport()">下一页</button></div>
      </template>
    </template>
  </section>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue';
import { pastedCases, statusLabels } from './evaluationHelpers.js';
const props = defineProps({ projectId: String, projects: {type: Array, default: () => []} });
const fields = ref({}), models = ref([]), csrf = ref(''), tab = ref('cases'), busy = ref(false), error = ref(''), message = ref('');
const filters = ref({q:'', category:'', scene:'', status:'', kind:''}), appliedFilters = ref({...filters.value});
const items = ref([]), total = ref(0), page = ref(1), categories = ref([]), scenes = ref([]), selected = ref([]), allFiltered = ref(false);
const operation = ref(''), editor = ref({}), pasteText = ref(''), conflict = ref('skip'), preview = ref(null);
const file = ref(null), sheets = ref([]), sheet = ref(''), headers = ref([]), headerRow = ref(1), mapping = ref({});
const bulkField = ref('status'), bulkStatus = ref('enabled'), bulkValue = ref(''), targetProject = ref(''), transferAction = ref('move');
const settings = ref({modelId:'default', judgeModelId:'default', threshold:80, limit:8, contextMaxChars:24000, contextReserveChars:2000, useIntent:true, useSynonyms:true});
try { Object.assign(settings.value, JSON.parse(localStorage.getItem(`evaluation-settings:${props.projectId}`) || '{}')); } catch {}
const runs = ref([]), runPage = ref(1), runTotal = ref(0), report = ref(null), reportId = ref(''), results = ref([]), resultId = ref(null), resultPage = ref(1), resultTotal = ref(0);
const resultFilters = ref({q:'', status:'', review:''}), reviewForm = ref({});
const operationTitles = {edit:'维护标准问答', paste:'批量增加问答', import:'Excel 导入', bulk:'批量修改', transfer:'跨项目移动 / 复制', delete:'确认删除', evaluate:'运行问答评测'};
let timer, disposed = false;
const selectedCount = computed(() => allFiltered.value ? total.value : selected.value.length);
const currentResult = computed(() => results.value.find(r => r.id === resultId.value));
const label = value => statusLabels[value] || value;
const reviewLabel = value => !value || value === 'pending' ? '待审查' : label(value);
const date = value => value ? new Date(value).toLocaleString('zh-CN') : '—';
const percent = value => `${Math.round((value || 0) * 1000) / 10}%`;
const isActive = run => ['queued','running'].includes(run.status);
const modelService = id => { const m = models.value.find(x => x.id === id); return m ? `${m.apiBase}${m.hasApiKey ? '' : '（未配置凭证）'}` : ''; };
const path = suffix => `/projects/${props.projectId}${suffix}`;
const selection = () => ({ids:[...selected.value], all_filtered:allFiltered.value, filters:{...appliedFilters.value}});
const countsText = counts => Object.entries(counts).map(([k,v]) => `${({total:'总计',pending:'待核对'}[k] || label(k))} ${v}`).join(' · ');
async function request(url, data, form = false) {
  const res = await fetch(`/api/evaluation${url}`, {method:data === undefined ? 'GET':'POST', credentials:'include', cache:'no-store',
    headers:data === undefined ? {} : {'X-CSRFToken':csrf.value, ...(form ? {} : {'Content-Type':'application/json'})}, body:data === undefined ? undefined : form ? data : JSON.stringify(data)});
  const json = await res.json().catch(() => ({error:`请求失败（${res.status}），请刷新后重试`}));
  if (!res.ok) throw new Error(json.error || '请求失败');
  return json;
}
async function perform(fn) { if (busy.value) return; busy.value=true; error.value=''; message.value=''; try { await fn(); } catch(e) {error.value=e.message;} finally {busy.value=false;} }
async function loadCases() { const data = await request(path(`/cases/?${new URLSearchParams({...appliedFilters.value, page:page.value})}`)); items.value=data.items; total.value=data.total; categories.value=data.categories; scenes.value=data.scenes; }
async function loadRuns() { try { const data = await request(path(`/runs/?page=${runPage.value}`)); runs.value=data.items; runTotal.value=data.total; } catch(e) {error.value=e.message;} }
function clearSelection() { selected.value=[]; allFiltered.value=false; }
function selectPage() { allFiltered.value=false; selected.value=items.value.map(i=>i.id); }
function selectAll() { selected.value=[]; allFiltered.value=true; }
function toggleCase(id) { if(allFiltered.value) {allFiltered.value=false; selected.value=items.value.map(i=>i.id);} selected.value=selected.value.includes(id) ? selected.value.filter(x=>x!==id) : [...selected.value,id]; }
function applyFilters() { perform(async()=>{appliedFilters.value={...filters.value}; page.value=1; clearSelection(); closeOperation(); await loadCases();}); }
function openOperation(name) { operation.value=name; preview.value=null; error.value=''; }
function closeOperation() { operation.value=''; preview.value=null; }
function editCase(item) { editor.value=item ? {...item} : {question:'', expected:'', kind:'answer', status:'enabled'}; openOperation('edit'); }
function saveCase() { perform(async()=>{await request(path('/cases/'), editor.value); closeOperation(); await loadCases(); message.value='标准问答已保存，历史评测不受影响。';}); }
function updateBulk() { perform(async()=>{const data=await request(path('/bulk/'), {...selection(), action:'update', changes:{[bulkField.value]:bulkField.value==='status'?bulkStatus.value:bulkValue.value}}); clearSelection(); closeOperation(); await loadCases(); message.value=`已更新 ${data.count} 条`;}); }
function deleteBulk() { perform(async()=>{const data=await request(path('/bulk/'), {...selection(), action:'delete', confirmed_count:selectedCount.value}); clearSelection(); closeOperation(); page.value=1; await loadCases(); message.value=`已删除 ${data.count} 条，历史评测快照保留。`;}); }
function previewPaste() { perform(async()=>{preview.value=await request(path('/imports/'), {rows:pastedCases(pasteText.value), conflict:conflict.value});}); }
function uploadForm(withMapping=false) { const form=new FormData(); form.append('file',file.value); if(sheet.value) form.append('sheet',sheet.value); form.append('header_row',headerRow.value); if(withMapping) {form.append('mapping',JSON.stringify(mapping.value)); form.append('conflict',conflict.value);} return form; }
function chooseFile(event) { file.value=event.target.files[0]; sheet.value=''; headers.value=[]; mapping.value={}; preview.value=null; sheets.value=[]; if(file.value) perform(async()=>{const data=await request(path('/imports/'),uploadForm(),true); sheets.value=data.sheets;}); }
function inspectSheet() { headers.value=[]; preview.value=null; mapping.value={}; if(sheet.value && file.value) perform(async()=>{const data=await request(path('/imports/'),uploadForm(),true); headers.value=data.headers; mapping.value=data.mapping;}); }
function previewExcel() { perform(async()=>{preview.value=await request(path('/imports/'),uploadForm(true),true);}); }
function previewTransfer() { perform(async()=>{preview.value=await request(path('/transfer/'), {...selection(), target:targetProject.value, action:transferAction.value});}); }
function commitPreview() { perform(async()=>{const data=await request(`/drafts/${preview.value.draft_id}/commit/`, {}); message.value=`操作完成：${countsText(data.counts)}`; clearSelection(); closeOperation(); page.value=1; await loadCases();}); }
function startRun() { perform(async()=>{const data=await request(path('/runs/'), {...selection(), all_filtered:selectedCount.value?allFiltered.value:true, config:settings.value}); localStorage.setItem(`evaluation-settings:${props.projectId}`,JSON.stringify(settings.value)); closeOperation(); tab.value='runs'; runPage.value=1; await loadRuns(); message.value=`评测已创建：${data.run.summary.total} 题。`;}); }
function cancelRun(id) { perform(async()=>{const data=await request(`/runs/${id}/`, {action:'cancel'}); message.value=data.message; await loadRuns();}); }
function retryRun(id) { perform(async()=>{await request(`/runs/${id}/`, {action:'retry'}); runPage.value=1; await loadRuns(); message.value='已创建重试批次，原报告保持不变。';}); }
function chooseResult(item) { resultId.value=item.id; reviewForm.value={status:item.review.status || 'pending', score:item.review.score ?? item.score.total ?? 0, passed:item.review.passed ?? item.score.passed ?? false, comment:item.review.comment || '', revision:item.review_revision}; }
async function loadReport() { try { if(!reportId.value) return; const data=await request(`/runs/${reportId.value}/?${new URLSearchParams({...resultFilters.value,page:resultPage.value})}`); report.value=data.run; results.value=data.items; resultTotal.value=data.total; if(!results.value.some(r=>r.id===resultId.value)) {resultId.value=null; if(results.value.length) chooseResult(results.value[0]);} } catch(e) {error.value=e.message;} }
async function openReport(id) { reportId.value=id; resultId.value=null; resultPage.value=1; resultFilters.value={q:'',status:'',review:''}; await loadReport(); tab.value='report'; }
function saveReview() { const id=resultId.value; const body={...reviewForm.value}; perform(async()=>{const data=await request(`/runs/${reportId.value}/results/${id}/review/`,body); const index=results.value.findIndex(r=>r.id===id); if(index>=0) results.value[index]=data.item; chooseResult(data.item); await loadReport(); message.value='人工复核已保存；原始自动评分未修改。';}); }
function download(url, name) { perform(async()=>{const res=await fetch(`/api/evaluation${url}`,{credentials:'include',cache:'no-store'}); if(!res.ok) throw new Error((await res.json().catch(()=>({}))).error || '报告下载失败'); const blob=await res.blob(); const link=document.createElement('a'); link.href=URL.createObjectURL(blob); link.download=name; link.click(); setTimeout(()=>URL.revokeObjectURL(link.href),1000);}); }
function refresh() { perform(async()=>{if(props.projectId) {await loadCases(); await loadRuns(); await loadReport();}}); }
onMounted(async()=>{await perform(async()=>{const data=await request('/config/'); fields.value=data.fields; models.value=data.models; csrf.value=data.csrfToken; if(props.projectId) {await loadCases(); await loadRuns();}}); async function poll() { if(disposed) return; if(!busy.value) {if(tab.value==='runs' && runs.value.some(isActive)) await loadRuns(); if(tab.value==='report' && report.value && isActive(report.value)) await loadReport();} if(!disposed) timer=setTimeout(poll,4000); } timer=setTimeout(poll,4000); });
onUnmounted(()=>{disposed=true; clearTimeout(timer);});
</script>

<style scoped>
.eval-panel{color:#20334d;min-width:0}.eval-heading{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}.eval-heading h2{font-size:25px;margin:5px 0}.eval-heading p,.eval-muted{color:#64748b;font-size:13px}.eval-eyebrow{color:#54759c;font-size:12px;letter-spacing:1px}.eval-tabs{display:flex;gap:8px;border-bottom:1px solid #dce4ed;margin-bottom:18px;padding-bottom:10px}.eval-tabs button.active,.eval-result-list button.active{background:#e7f0ff;border-color:#719bd3;color:#17477f}.eval-tabs span{margin-left:8px;color:#64748b}.eval-panel button{cursor:pointer;border:1px solid #cbd7e4;background:white;color:#294667;border-radius:7px;padding:8px 12px;font-size:13px}.eval-panel button.primary{background:#255b92;color:white;border-color:#255b92}.eval-panel button:disabled{opacity:.45;cursor:not-allowed}.eval-panel input:not([type=checkbox]),.eval-panel select,.eval-panel textarea{border:1px solid #cbd7e4;border-radius:6px;padding:9px;background:white;color:#20334d;max-width:100%;font:inherit;box-sizing:border-box}.eval-panel textarea{width:100%;resize:vertical;line-height:1.7}.eval-panel input[type=checkbox]{width:16px;height:16px}.eval-filters,.eval-actions,.eval-selection{display:flex;gap:9px;align-items:center;flex-wrap:wrap;margin:14px 0}.eval-filters input{flex:1;min-width:180px}.eval-filters select{max-width:180px}.eval-selection{font-size:13px;background:#f2f6fb;padding:10px;border-radius:8px}.eval-selection button{padding:5px 9px}.eval-operation{background:#fff;border:1px solid #b8cde4;border-radius:10px;padding:20px;margin:18px 0}.eval-operation>header{display:flex;align-items:center;justify-content:space-between}.eval-operation h3{margin:0 0 10px}.eval-operation p{font-size:13px;line-height:1.8}.eval-form{display:grid;grid-template-columns:1fr 1fr;gap:16px}.eval-form label,.eval-mapping label{display:flex;flex-direction:column;gap:7px;font-size:13px}.eval-form .wide{grid-column:1/-1}.eval-form .eval-check{flex-direction:row;align-items:center}.eval-mapping{display:grid;grid-template-columns:repeat(3,minmax(0,1fr));gap:12px;margin:18px 0}.eval-table-wrap{overflow:auto;border:1px solid #dce4ed;border-radius:9px;background:white}.eval-panel table{border-collapse:collapse;width:100%;font-size:13px}.eval-panel th{background:#edf3f9;text-align:left;font-weight:600;color:#455f7e;white-space:nowrap}.eval-panel th,.eval-panel td{padding:13px 12px;border-bottom:1px solid #e7edf4;vertical-align:top}.eval-panel td{max-width:450px;overflow-wrap:anywhere}.eval-panel td.eval-question{min-width:180px;white-space:pre-wrap}.eval-panel small{display:block;color:#73849a;font-size:12px;margin-top:6px;overflow-wrap:anywhere}.eval-pager{display:flex;justify-content:flex-end;align-items:center;gap:15px;margin-top:16px;font-size:13px}.eval-notice{background:#edf5ff;border-left:3px solid #4b83bc;padding:12px;overflow-wrap:anywhere}.eval-error{background:#fff0ee;color:#ac3c32;padding:12px;border-radius:6px;white-space:pre-wrap;overflow-wrap:anywhere}.eval-panel .eval-danger{background:#ae4035;color:white}.eval-preview{border-top:1px solid #dce4ed;margin-top:20px;padding-top:12px}.eval-empty{text-align:center;color:#7c8da3;padding:35px!important}.eval-run{border:1px solid #dce4ed;background:white;border-radius:10px;padding:20px;margin:14px 0}.eval-badge{background:#eaf0f6;border-radius:12px;padding:4px 9px;font-size:12px;margin-left:12px}.eval-metrics{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin:20px 0}.eval-metrics>div{background:white;border:1px solid #dce4ed;border-radius:10px;padding:18px}.eval-metrics strong{font-size:29px;display:block;color:#244f80}.eval-metrics span{display:block;font-size:12px;color:#6a7c91;margin-top:10px}.eval-panel details{background:#f5f8fc;border:1px solid #e0e8f1;border-radius:7px;padding:13px;margin:15px 0;overflow-wrap:anywhere}.eval-panel summary{cursor:pointer;font-size:13px;font-weight:600}.eval-group-stats{display:flex;gap:12px;flex-wrap:wrap;margin:12px 0;font-size:12px}.eval-review-layout{display:grid;grid-template-columns:250px minmax(0,1fr);gap:18px}.eval-result-list{display:flex;flex-direction:column;gap:8px;align-self:start;max-height:900px;overflow:auto}.eval-result-list button{text-align:left;padding:13px;line-height:1.6}.eval-result-list strong{font-weight:500;display:-webkit-box;-webkit-line-clamp:3;-webkit-box-orient:vertical;overflow:hidden}.eval-result-detail{min-width:0}.eval-result-detail h3{font-size:18px;line-height:1.7;white-space:pre-wrap;overflow-wrap:anywhere}.eval-answer-pair{display:grid;grid-template-columns:1fr 1fr;gap:14px}.eval-answer-pair section{border:1px solid #d5e0eb;background:white;border-radius:9px;padding:18px;min-width:0}.eval-answer-pair h4{font-size:13px;color:#617d9c;margin:0 0 14px;padding-bottom:12px;border-bottom:1px solid #e8edf3}.eval-fulltext{white-space:pre-wrap;overflow-wrap:anywhere;line-height:1.85;font-size:14px}.eval-score{padding:18px;background:#f1f6fc;border-radius:8px;margin:16px 0;font-size:13px;line-height:1.7}.eval-score h4{margin:0 0 12px}.eval-score strong{font-size:22px;margin:0 12px;color:#245585}.eval-human{padding:20px;border:1px solid #bbd1e9;background:#fff;border-radius:9px}.eval-human h4{margin:0}.eval-human p{font-size:13px;color:#697c92}.eval-human button{margin-top:15px}.eval-history{padding:10px 0;border-bottom:1px solid #dce4ed}@media(max-width:1150px){.eval-review-layout{grid-template-columns:190px minmax(0,1fr)}.eval-answer-pair{grid-template-columns:1fr}.eval-metrics{grid-template-columns:1fr 1fr}}@media(max-width:700px){.eval-review-layout,.eval-form,.eval-mapping{grid-template-columns:1fr}.eval-result-list{max-height:260px}.eval-form .wide{grid-column:auto}}
</style>
