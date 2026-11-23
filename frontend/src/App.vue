<template>
  <main class="shell">
    <template v-if="currentUser.authenticated">
    <header class="topbar">
      <div class="topbar-brand">
        <p class="eyebrow">LLM Knowledge Base</p>
        <h1>LLM 知识库</h1>
      </div>

      <div class="topbar-project">
        <select v-model="selectedProjectId" @change="handleProjectChange">
          <option disabled value="">选择本地知识项目</option>
          <option v-for="project in projects" :key="project.id || project.path || project.name" :value="project.id || project.path">
            {{ project.name || project.title || project.path || project.id }}
          </option>
        </select>
        <button @click="loadProjects" :disabled="loading.projects" title="刷新项目列表">刷新</button>
        <div class="popover-anchor">
          <button @click="projectMenuOpen = !projectMenuOpen">定时刷新</button>
          <div v-if="projectMenuOpen" class="popover-mask" @click="projectMenuOpen = false"></div>
          <div v-if="projectMenuOpen" class="popover project-popover">
            <button @click="projectMenuOpen = false; refreshCurrentProject()" :disabled="!selectedProjectId || loading.projects || loading.graph || loading.search">
              刷新当前项目
            </button>
            <label class="project-refresh-interval">
              <span>定时刷新</span>
              <select v-model.number="projectRefreshMinutes" @change="handleProjectRefreshIntervalChange">
                <option :value="0">关闭</option>
                <option :value="1">1 分钟</option>
                <option :value="5">5 分钟</option>
                <option :value="10">10 分钟</option>
                <option :value="30">30 分钟</option>
              </select>
            </label>
          </div>
        </div>
      </div>

      <div class="topbar-right">
        <div class="view-tabs space-tabs">
          <button :class="{ active: activeSpace === 'query' }" @click="switchSpace('query')">查询</button>
          <button v-if="currentUser.isAdmin" :class="{ active: activeSpace === 'admin' }" @click="switchSpace('admin')">管理控制台</button>
        </div>
        <div class="popover-anchor">
          <button class="account-trigger" @click="accountMenuOpen = !accountMenuOpen">
            <span>{{ currentUser.authenticated ? currentUser.username : "匿名" }}</span>
            <span v-if="currentUser.isAdmin" class="account-badge">管理员</span>
          </button>
          <div v-if="accountMenuOpen" class="popover-mask" @click="accountMenuOpen = false"></div>
          <div v-if="accountMenuOpen" class="popover account-menu">
            <p class="account-line">
              <strong>{{ currentUser.authenticated ? currentUser.username : "未登录（匿名）" }}</strong>
              <span v-if="currentUser.authenticated" :class="['account-badge', { muted: !currentUser.isAdmin }]">
                {{ currentUser.isAdmin ? "管理员" : "普通用户" }}
              </span>
            </p>
            <button v-if="currentUser.authenticated" class="account-btn" @click="accountMenuOpen = false; submitLogout()">退出登录</button>
            <button v-else class="account-btn" @click="accountMenuOpen = false; openLoginDialog()">管理员登录</button>
          </div>
        </div>
      </div>
    </header>

    <p v-if="status" class="status status-bar">{{ status }}</p>

    <section class="workspace">
      <template v-if="activeSpace === 'query'">
      <div class="view-tabs">
        <button :class="{ active: activeView === 'qa' }" @click="switchView('qa')">问答</button>
        <button :class="{ active: activeView === 'graph' }" @click="switchView('graph')">关系图谱</button>
      </div>

      <template v-if="activeView === 'qa'">
      <form class="query-bar" @submit.prevent="searchAndComposeTopThree">
        <textarea v-model="query" placeholder="输入你想查阅和综合的问题，例如：登录接口鉴权失败时应该检查哪些配置？" />
        <DocumentVersions v-if="selectedProjectId" :project-id="selectedProjectId" :refresh-key="versionRefreshKey" :busy="loading.search || loading.compose" v-model="versionScope" />
        <div v-if="searchRuntimeIndex?.queryRoles && lastSearchedQuestion === query" class="settings-section" aria-label="问题解析">
          <h3>问题解析</h3>
          <p>核心对象：{{ searchRuntimeIndex.queryRoles.coreObjects?.join("、") || "暂未明确" }} · 范围：{{ searchRuntimeIndex.queryRoles.scopeTerms?.join("、") || "未指定" }} · 询问属性：{{ searchRuntimeIndex.queryRoles.requestedAttribute || "未明确" }}</p>
          <details><summary>查看分词依据（jieba＋领域词典）</summary>
            <p v-for="(token, index) in searchRuntimeIndex.queryRoles.segmentation || []" :key="index">{{ token.text }} · {{ token.protected ? "保护词" : token.role === "attribute" ? "属性候选" : token.role === "candidate" ? "实词候选" : "不用于独立召回" }}</p>
          </details>
        </div>
        <div v-if="showSearchTimeline" class="search-timeline">
          <button type="button" class="timeline-summary" @click="timelineOpen = !timelineOpen">
            <span class="timeline-label">检索过程</span>
            <span class="timeline-text">{{ searchTimelineSummary }}</span>
            <i class="timeline-toggle">{{ timelineOpen ? "−" : "+" }}</i>
          </button>
          <div v-show="timelineOpen" class="timeline-detail">
            <div v-if="synonymExpansion.expandedTerms?.length" class="synonym-strip">
              <span>已扩展</span>
              <b v-for="term in synonymExpansion.expandedTerms" :key="term">{{ term }}</b>
            </div>
            <div v-if="showIntentStrip" class="synonym-strip">
              <span>意图 {{ queryIntent.intent || "general" }}</span>
              <b v-for="term in queryIntentTerms" :key="term">{{ term }}</b>
              <em v-if="queryIntent.reason">{{ queryIntent.reason }}</em>
            </div>
            <div v-if="showRerankStrip" class="rerank-strip">
              <span>{{ llmRerank.enabled ? "LLM 已筛选" : "LLM 筛选未启用" }}</span>
              <b v-if="llmRerank.threshold !== undefined">阈值 {{ llmRerank.threshold }}</b>
              <em v-if="llmRerank.reason">{{ llmRerank.reason }}</em>
            </div>
            <div v-if="searchRuntimeIndex?.ok" class="synonym-strip">
              <span>索引检索</span>
              <b>{{ searchRuntimeIndex.retrievalMode === "object-linked" ? "对象直连原文" : searchRuntimeIndex.retrievalMode === "object-source-local-fts" ? "关联原文局部 FTS" : "全项目 FTS" }}</b>
              <b v-if="searchRuntimeIndex.elapsedMs !== undefined">{{ searchRuntimeIndex.elapsedMs }}ms</b>
              <em v-if="searchRuntimeIndex.globalFtsUsed">{{ searchRuntimeIndex.fallbackReason || "证据不足，已执行全项目兜底" }}</em>
              <em v-else>已跳过全项目 FTS</em>
            </div>
          </div>
        </div>
        <div class="actions">
          <div class="popover-anchor">
            <button type="button" @click="searchSettingsOpen = !searchSettingsOpen">
              检索设置<template v-if="searchSettingsEnabledCount"> · {{ searchSettingsEnabledCount }} 项已启用</template>
            </button>
            <div v-if="searchSettingsOpen" class="popover-mask" @click="searchSettingsOpen = false"></div>
            <div v-if="searchSettingsOpen" class="popover settings-popover">
              <div class="settings-section">
                <h3>模型选择</h3>
                <p class="settings-hint">用于检索问答和知识摄入抽取。</p>
                <label class="field">
                  <span>当前模型</span>
                  <select v-model="selectedModelId">
                    <option v-for="model in llmModels" :key="model.id" :value="model.id">
                      {{ model.name || model.model }}
                    </option>
                  </select>
                </label>
                <div class="model-test-row">
                  <button type="button" @click="testSelectedModel" :disabled="loading.modelTest || !selectedModelId">
                    {{ loading.modelTest ? "测试中..." : "测试当前模型" }}
                  </button>
                  <span v-if="modelTestResult" :class="['model-test-result', modelTestResult.ok ? 'ok' : 'fail']">
                    {{ modelTestResultText }}
                  </span>
                </div>
              </div>
              <div class="settings-section">
                <h3>回答配置</h3>
                <label class="field"><span>证据上下文总预算（字符）</span><input v-model.number="contextMaxChars" type="number" min="1000" max="2000000" step="1000" /></label>
                <label class="field"><span>每条证据预留预算（字符）</span><input v-model.number="contextReserveChars" type="number" min="0" max="2000000" step="500" /></label>
                <p class="settings-hint">默认总计 24,000 字符，每条预留 2,000 字符；总预算不足时均分预留。字符数不等于 Token，请为问题、提示词和模型输出留出空间。设置保存在当前浏览器。</p>
                <h3>检索设置</h3>
                <label class="field">
                  <span>检索模式</span>
                  <select v-model="mode">
                    <option value="hybrid">hybrid</option>
                    <option value="deep">deep</option>
                    <option value="keyword">keyword</option>
                  </select>
                </label>
                <label class="field">
                  <span>结果数量</span>
                  <input v-model.number="limit" type="number" min="1" max="20" />
                </label>
                <label class="check-field">
                  <input type="checkbox" v-model="useSynonyms" />
                  <span>启用 synonym.yaml 扩展</span>
                </label>
                <label class="check-field">
                  <input type="checkbox" v-model="useIntent" />
                  <span>启用 LLM 意图识别</span>
                </label>
                <label class="check-field">
                  <input type="checkbox" v-model="rerankWithLlm" />
                  <span>启用 LLM 相似度筛选</span>
                </label>
                <label class="check-field">
                  <input type="checkbox" v-model="supplementRawSearch" />
                  <span>补充原文搜索</span>
                </label>
                <label v-if="rerankWithLlm" class="field">
                  <span>相似度阈值</span>
                  <input v-model.number="rerankThreshold" type="number" min="0" max="1" step="0.05" />
                </label>
              </div>
            </div>
          </div>
          <button type="submit" class="primary" :disabled="!canQuery || loading.search || loading.compose">
            检索并回答
          </button>
          <button type="button" @click="runSearch" :disabled="!canQuery || loading.search">单检索</button>
        </div>
      </form>

      <div v-if="!results.length && !answer && !loading.search && !loading.compose" class="example-questions">
        <span>试试这样问：</span>
        <button
          v-for="question in exampleQuestions"
          :key="question"
          type="button"
          :disabled="!selectedProjectId"
          :title="selectedProjectId ? '' : '请先选择项目'"
          @click="askExample(question)"
        >{{ question }}</button>
      </div>

      <div class="columns">
        <section class="results panel">
          <div class="panel-head">
            <div>
              <h2>证据文档</h2>
              <span v-if="results.length">已选 {{ selectedEvidenceKeys.length }} / {{ results.length }}</span>
            </div>
            <div class="actions compact">
              <button class="primary" @click="composeAnswer" :disabled="!canQuery || !selectedEvidenceKeys.length || loading.compose">
                按选择的回答
              </button>
              <button @click="selectTopResults(3)" :disabled="!results.length">选前三</button>
              <button @click="clearSelection" :disabled="!selectedEvidenceKeys.length">清空选择</button>
            </div>
          </div>

          <div v-if="!results.length" class="empty">还没有检索结果。</div>
          <article
            v-for="item in results"
            :key="itemKey(item)"
            class="result-row"
            :class="{ selected: isResultSelected(item), flashing: flashingEvidenceKey === itemKey(item) }"
            :ref="el => setEvidenceEl(itemKey(item), el)"
            @click="toggleResultSelection(item)"
          >
            <label @click.stop>
              <input type="checkbox" :value="itemKey(item)" v-model="selectedEvidenceKeys" @click.stop />
              <span>{{ itemTitle(item) }}</span>
              <button type="button" class="doc-open-btn" @click.stop="openEvidenceDoc(item)">查看本节全文</button>
              <span v-if="isResultSelected(item)" class="result-check">✓ 已选</span>
            </label>
            <div class="result-evidence-meta">
              <span :class="['evidence-badge', ['source', 'raw-source'].includes(item.evidenceKind) ? 'raw' : 'summary']">
                {{ ["source", "raw-source"].includes(item.evidenceKind) ? "原文片段" : "摘要页" }}
              </span>
              <span v-if="item.versionLabel" class="relevance-badge">{{ item.documentName }} · {{ item.versionLabel }}</span>
              <span v-if="item.llmRelevance !== undefined && item.llmRelevance !== null" :class="['relevance-badge', relevanceLevelClass(item.llmRelevance)]">
                相关性 {{ formatRelevance(item.llmRelevance) }}
              </span>
              <span v-if="item.matchKind" class="relevance-badge">
                {{ item.matchKind === "object-source-structured" ? "知识对象直连章节" : item.matchKind === "object-name-match" ? "名称匹配（补充）" : item.matchKind === "exact-title" ? "标题精确命中" : item.matchKind === "exact-phrase" ? "短语命中" : "全文索引" }}
              </span>
              <span v-if="item.headingPath" class="relevance-badge">
                章节：{{ item.headingPath }}
              </span>
              <span v-else-if="item.chunkId" class="relevance-badge">
                Chunk #{{ item.chunkId }}<template v-if="item.chunkNo !== undefined && item.chunkNo !== null"> · 文档内第 {{ Number(item.chunkNo) + 1 }} 块</template>
              </span>
              <span v-if="item.evidenceSourcePath">{{ item.evidenceSourcePath }}</span>
              <span v-else>{{ itemPath(item) }}</span>
            </div>
            <div v-if="item.retrieval" class="retrieval-explanation">
              <span><b>召回原因：</b>{{ item.retrieval.reason }}</span>
              <span v-if="retrievalTerms(item).length"><b>命中词：</b>{{ retrievalTerms(item).join("、") }}</span>
              <span v-for="hit in item.retrieval.matchLocations || []" :key="`${hit.location}:${hit.term}`"><b>{{ hit.location }}命中：</b>{{ hit.term }}</span>
              <span v-if="item.retrieval.descriptionMatchedTerms?.length"><b>对象描述命中（间接关联）：</b>{{ item.retrieval.descriptionMatchedTerms.join("、") }}</span>
              <span v-if="item.retrieval.outsidePreviewTerms?.length"><b>正文其他位置命中：</b>{{ item.retrieval.outsidePreviewTerms.join("、") }}（当前预览未覆盖，可查看本节全文）</span>
              <span v-if="expandedMatchedTerms(item).length"><b>扩展词：</b>{{ expandedMatchedTerms(item).join("、") }}</span>
              <span v-if="item.retrieval.chain?.length"><b>关联链路：</b>{{ item.retrieval.chain.join(" → ") }}</span>
            </div>
            <p class="result-snippet">
              <template v-for="(part, index) in itemSnippetParts(item)" :key="index">
                <strong v-if="part.hit">{{ part.text }}</strong>
                <span v-else>{{ part.text }}</span>
              </template>
            </p>
          </article>
        </section>

        <section class="answer panel" ref="answerPanelEl">
          <div class="panel-head">
            <h2>回答</h2>
            <span v-if="answerMeta">{{ answerMeta }}</span>
          </div>
          <details v-if="thinking" class="thinking" :open="!answer">
            <summary>思考过程</summary>
            <pre>{{ thinking }}</pre>
          </details>
          <div v-if="answer" class="markdown-answer">
            <template v-for="(block, blockIndex) in renderedAnswerBlocks" :key="blockIndex">
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
                    <span v-else-if="part.type === 'link'">{{ part.text }}</span>
                    <button
                      v-else-if="part.type === 'cite'"
                      type="button"
                      class="answer-cite"
                      :class="{ inactive: !citeTargetPath(part) }"
                      :title="citeTargetPath(part) ? '定位到证据' : '该编号没有对应证据'"
                      @click="scrollToEvidence(part)"
                    >{{ part.text }}</button>
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
                  <strong v-if="part.type === 'strong'">{{ part.text }}</strong>
                  <code v-else-if="part.type === 'code'">{{ part.text }}</code>
                  <span v-else>{{ part.text }}</span>
                </template>
              </blockquote>
              <p v-else>
                <template v-for="(part, partIndex) in block.parts" :key="partIndex">
                  <strong v-if="part.type === 'strong'">{{ part.text }}</strong>
                  <code v-else-if="part.type === 'code'">{{ part.text }}</code>
                  <span v-else-if="part.type === 'link'">{{ part.text }}</span>
                  <button
                    v-else-if="part.type === 'cite'"
                    type="button"
                    class="answer-cite"
                    :class="{ inactive: !citeTargetPath(part) }"
                    :title="citeTargetPath(part) ? '定位到证据' : '该编号没有对应证据'"
                    @click="scrollToEvidence(part)"
                  >{{ part.text }}</button>
                  <span v-else>{{ part.text }}</span>
                </template>
              </p>
            </template>
            <p v-if="loading.compose" class="streaming-hint">{{ composeStage || "正在继续生成..." }}</p>
          </div>
          <div v-else-if="loading.compose" class="empty">{{ composeStage || "正在读取文档并生成回答..." }}</div>
          <div v-else-if="prompt" class="fallback">
            <p>未配置 LLM key，下面是后端已拼接好的提示词。</p>
            <pre>{{ prompt }}</pre>
          </div>
          <div v-else class="empty">生成后会显示在这里。</div>
        </section>
      </div>

      <template v-if="evidenceDocItem">
        <div class="doc-drawer-mask" @click="closeEvidenceDoc"></div>
        <aside class="doc-drawer">
          <div class="doc-drawer-head">
            <div>
              <strong>{{ itemTitle(evidenceDocItem) }}</strong>
              <p v-if="evidenceDocItem.headingPath">章节：{{ evidenceDocItem.headingPath }}</p>
              <p>{{ itemPath(evidenceDocItem) || evidenceDocItem.evidenceSourcePath }}</p>
            </div>
            <button class="drawer-close" @click="closeEvidenceDoc" title="关闭">×</button>
          </div>
          <div class="doc-drawer-body">
            <div v-if="loading.evidenceDoc" class="empty compact-empty">正在读取文档内容...</div>
            <div v-else-if="evidenceDocError" class="node-content-error">{{ evidenceDocError }}</div>
            <MarkdownBlocks v-else-if="evidenceDocBlocks.length" :blocks="evidenceDocBlocks" />
            <div v-else class="empty compact-empty">没有可读取的正文。</div>
          </div>
        </aside>
      </template>

      </template>

      <section v-else-if="activeView === 'graph'" class="graph-panel panel">
        <div class="panel-head graph-head">
          <div>
            <h2>知识关系图谱</h2>
            <p>{{ graphSummary }}</p>
          </div>
          <div class="actions compact">
            <button :class="{ primary: graphMode === 'relations' }" @click="graphMode = 'relations'">原关系图</button>
            <button :class="{ primary: graphMode === 'structure' }" @click="graphMode = 'structure'">章节证据图</button>
            <button @click="loadGraph" :disabled="!selectedProjectId || loading.graph">刷新图谱</button>
            <button @click="resetGraphView" :disabled="!graphModel.nodes.length">适应画布</button>
            <button @click="zoomGraphBy(1.16)" :disabled="!graphModel.nodes.length">放大</button>
            <button @click="zoomGraphBy(0.86)" :disabled="!graphModel.nodes.length">缩小</button>
          </div>
        </div>

        <div v-if="graphError" class="graph-error">{{ graphError }}</div>

        <div class="graph-stage">
          <div v-if="loading.graph" class="empty graph-loading">正在读取知识图谱...</div>
          <div v-else-if="!graphModel.nodes.length" class="empty graph-loading">还没有图谱数据。</div>
          <svg
            v-else
            class="graph-canvas"
            :viewBox="graphViewBox"
            role="img"
            aria-label="knowledge claim graph"
            @wheel.prevent="handleGraphWheel"
            @pointerdown="startGraphPan"
            @pointermove="handleGraphPointerMove"
            @pointerup="endGraphDrag"
            @pointerleave="endGraphDrag"
          >
            <g class="graph-viewport" :transform="graphTransform">
              <line
                v-for="link in visibleGraph.links"
                :key="link.id"
                class="graph-link"
                :class="{ dimmed: selectedGraphNode && !linkTouchesNode(link, selectedGraphNode.id) }"
                :x1="link.sourceNode.x"
                :y1="link.sourceNode.y"
                :x2="link.targetNode.x"
                :y2="link.targetNode.y"
              />
              <g
                v-for="node in visibleGraph.nodes"
                :key="node.id"
                class="graph-node"
                :class="{ selected: selectedGraphNode?.id === node.id, dimmed: selectedGraphNode && !isNodeConnected(node.id, selectedGraphNode.id) }"
                :transform="`translate(${node.x}, ${node.y})`"
                @pointerdown.stop="startNodeDrag($event, node)"
                @click="selectGraphNode(node)"
              >
                <circle :r="node.radius" :fill="graphColor(node.type)" />
                <text v-if="node.showLabel || selectedGraphNode?.id === node.id" :x="node.radius + 7" y="4">{{ node.label }}</text>
              </g>
            </g>
          </svg>

          <div v-if="graphModel.nodes.length" class="graph-dock">
          <aside class="graph-hud">
            <div class="graph-stat-grid">
              <span>节点 <b>{{ graphModel.nodes.length }}</b></span>
              <span>连接 <b>{{ graphModel.links.length }}</b></span>
            </div>

            <div class="graph-filter">
              <h3>节点类型</h3>
              <label v-for="type in graphTypes" :key="type" class="type-row">
                <input type="checkbox" :value="type" v-model="selectedGraphTypes" />
                <i :style="{ background: graphColor(type) }"></i>
                <span>{{ type }}</span>
                <b>{{ graphTypeCounts[type] || 0 }}</b>
              </label>
            </div>

            <p class="graph-hint">滚轮缩放 · 点击节点查看详情</p>
          </aside>

          <aside class="graph-node-index">
            <div class="graph-node-index-head">
              <h3>节点索引</h3>
              <span>{{ visibleGraph.nodes.length }}</span>
            </div>
            <div class="graph-node-groups">
              <section v-for="group in graphNodeGroups" :key="group.key" class="graph-node-group">
                <button class="graph-node-group-head" @click="toggleGraphNodeGroup(group.key)">
                  <span>{{ group.key }}</span>
                  <b>{{ group.nodes.length }}</b>
                  <i>{{ collapsedGraphNodeGroups[group.key] ? "+" : "−" }}</i>
                </button>
                <div v-show="!collapsedGraphNodeGroups[group.key]" class="graph-node-group-items">
                  <button
                    v-for="node in group.nodes"
                    :key="node.id"
                    :class="{ active: selectedGraphNode?.id === node.id }"
                    @click="selectGraphNodeFromIndex(node)"
                  >
                    <i :style="{ background: graphColor(node.type) }"></i>
                    <span>{{ node.label }}</span>
                  </button>
                </div>
              </section>
            </div>
          </aside>
          </div>

          <aside v-if="selectedGraphNode" class="node-drawer">
            <div class="node-drawer-head">
              <div>
                <strong>{{ selectedGraphNode.label }}</strong>
                <p>{{ selectedGraphNode.type }}</p>
              </div>
              <button class="drawer-close" @click="selectedGraphNode = null" title="关闭">×</button>
            </div>
            <div class="node-drawer-body">
              <dl class="node-meta">
                <template v-for="item in nodeDetailRows(selectedGraphNode)" :key="item.key">
                  <dt>{{ item.key }}</dt>
                  <dd>{{ item.value }}</dd>
                </template>
              </dl>
              <div class="node-content">
                <h3>节点内容</h3>
                <div v-if="loading.nodeContent" class="empty compact-empty">正在读取节点内容...</div>
                <div v-else-if="nodeContentError" class="node-content-error">{{ nodeContentError }}</div>
                <template v-else-if="selectedNodeContentBlocks.length">
                  <template v-for="(block, blockIndex) in selectedNodeContentBlocks" :key="blockIndex">
                    <h4 v-if="block.type === 'heading'" class="content-heading">
                      <template v-for="(part, partIndex) in block.parts" :key="partIndex">
                        <button v-if="part.type === 'link'" class="content-link" @click="selectGraphReference(part.ref)">
                          {{ part.text }}
                        </button>
                        <code v-else-if="part.type === 'code'">{{ part.text }}</code>
                        <strong v-else-if="part.type === 'strong'">{{ part.text }}</strong>
                        <span v-else>{{ part.text }}</span>
                      </template>
                    </h4>
                    <ul v-else-if="block.type === 'list'" class="node-markdown-list">
                      <li v-for="(item, itemIndex) in block.items" :key="itemIndex">
                        <template v-for="(part, partIndex) in item.parts" :key="partIndex">
                          <button v-if="part.type === 'link'" class="content-link" @click="selectGraphReference(part.ref)">
                            {{ part.text }}
                          </button>
                          <code v-else-if="part.type === 'code'">{{ part.text }}</code>
                          <strong v-else-if="part.type === 'strong'">{{ part.text }}</strong>
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
                        <button v-if="part.type === 'link'" class="content-link" @click="selectGraphReference(part.ref)">
                          {{ part.text }}
                        </button>
                        <code v-else-if="part.type === 'code'">{{ part.text }}</code>
                        <strong v-else-if="part.type === 'strong'">{{ part.text }}</strong>
                        <span v-else>{{ part.text }}</span>
                      </template>
                    </p>
                  </template>
                </template>
                <div v-else class="empty compact-empty">这个节点没有可读取的正文。</div>
              </div>
            </div>
          </aside>
        </div>
      </section>

      </template>

      <div v-else class="admin-layout">
        <nav class="admin-nav">
          <button
            v-for="item in adminNavItems"
            :key="item.key"
            :class="{ active: activeView === item.key }"
            @click="switchView(item.key)"
          >
            {{ item.label }}
          </button>
        </nav>

        <div class="admin-content">
          <EvaluationPanel v-if="activeAdminPage === 'evaluation' && currentUser.isAdmin" :key="selectedProjectId" :project-id="selectedProjectId" :projects="projects" />
          <ProjectRegistry v-if="activeAdminPage === 'projects'" :projects="registeredProjects" @refresh="loadProjects" @select="selectRegisteredProject" />
          <IngestionPromptManager v-else-if="activeAdminPage === 'prompts'" />
          <section v-else-if="activeAdminPage === 'versions'" class="panel">
            <h2>文档版本管理</h2>
            <DocumentVersionLibrary :project-id="selectedProjectId" :refresh-key="versionRefreshKey" @preview="openEvidenceDoc" @changed="versionsChanged" @add-version="openVersionIntake" />
          </section>

          <section v-else-if="activeAdminPage === 'ingestion'" class="panel import-panel">
            <div class="panel-head"><div><h2>知识摄入</h2><p>选择项目，添加文件或目录，再确认文档归属与版本。新增文件只处理本次选择的内容。</p></div></div>
            <KnowledgeIntake ref="knowledgeIntakeRef" :projects="projects" :project-id="selectedProjectId" :preferred-document-id="preferredDocumentId" :busy="loading.importSubmit" />
            <div class="import-layout">
              <section class="import-form">
                <div class="form-section-title">
                  <h3>2. 抽取设置</h3>
                  <p>摘要按目录和标题章节自动切分，所有章节都会处理；超长章节由后端自动分段。</p>
                </div>
                <label class="field">
                  <span>抽取模型</span>
                  <select v-model="selectedModelId" :disabled="loading.importSubmit || !llmModels.length">
                    <option v-for="model in llmModels" :key="model.id" :value="model.id">
                      {{ model.name || model.model }}
                    </option>
                  </select>
                </label>
                <p class="settings-hint">与问答的模型选择同步，提交后用于本次摄入任务；切换不会影响已启动的任务。</p>
                <div class="import-two-col compact-inputs">
                  <label class="field">
                    <span>单任务 LLM 并发数</span>
                    <input v-model.number="wikiImport.llmConcurrency" type="number" min="1" max="32" />
                  </label>
                  <label class="field">
                    <span>抽取说明长度</span>
                    <select v-model="wikiImport.promptProfile">
                      <option value="compact">精简（推荐，缩短摘要和对象说明）</option>
                      <option value="standard">标准（较详细）</option>
                    </select>
                  </label>
                  <label class="field"><span>每次抽取最多正文字符</span><input v-model.number="wikiImport.extractionBatchChars" type="number" min="500" max="12000" /></label>
                  <label class="field"><span>每次抽取最多原文块</span><input v-model.number="wikiImport.extractionBatchSize" type="number" min="1" max="12" /></label>
                </div>
                <label class="check-field">
                  <input type="checkbox" v-model="wikiImport.allowHeuristic" />
                  <span>LLM 失败或未配置时允许规则降级（仅适合测试）</span>
                </label>
              </section>

              <aside class="import-summary">
                <div class="operation-card">
                  <div>
                    <strong>抽取知识</strong>
                    <p>基于 V7 标准 Chunk 抽取知识对象、Mention、Claim 和证据；Markdown 只是可删除的导出。</p>
                  </div>
                  <button @click="buildWikiLayer" :disabled="loading.importSubmit">
                    {{ loading.importSubmit ? "提交中..." : "开始摄入" }}
                  </button>
                </div>
                <div class="strategy-notes">
                  <h3>处理规则</h3>
                  <dl>
                    <dt>引用绑定</dt>
                    <dd>由程序写入 sources，不让 LLM 猜来源。</dd>
                    <dt>同名合并</dt>
                    <dd>同名对象统一写入 KnowledgeObject，别名写入 ObjectTerm。</dd>
                    <dt>章节覆盖</dt>
                    <dd>优先按标题层级切分，不限制每文档切片数量，日志会显示实际切片数。</dd>
                    <dt>原文优先</dt>
                    <dd>检索和回答直接读取结构节点与原文切片，不读取 Markdown 导出。</dd>
                  </dl>
                </div>
              </aside>
            </div>

            <section class="wiki-job-history">
              <div class="panel-head">
                <div>
                  <h2>摄入任务历史</h2>
                  <span>共 {{ wikiImportJobTotal }} 条，显示最近 {{ wikiImportJobs.length }} 条 · {{ activeWikiImportJobCount }} 个运行中</span>
                </div>
                <button @click="loadWikiImportJobs" :disabled="loading.importJobs">
                  {{ loading.importJobs ? "刷新中..." : "刷新" }}
                </button>
              </div>
              <div v-if="!wikiImportJobs.length" class="empty compact-empty">还没有知识摄入任务。</div>
              <div v-else class="wiki-job-list">
                <article
                  v-for="job in wikiImportJobs"
                  :key="job.id"
                  class="wiki-job-row"
                  :class="[`is-${job.status}`, { active: job.id === wikiImportJobId }]"
                >
                  <div class="wiki-job-main">
                    <div class="wiki-job-title">
                      <strong>{{ job.projectName || pathName(job.projectDir) || "未命名项目" }}</strong>
                      <span class="wiki-job-status">{{ wikiJobStatusText(job) }}</span>
                    </div>
                    <p>{{ job.projectDir }}</p>
                    <div class="wiki-job-meta">
                      <span v-if="job.modelName">模型：{{ job.modelName }}</span>
                      <span>开始：{{ formatJobTime(job.startedAt) }}</span>
                      <span v-if="job.finishedAt">结束：{{ formatJobTime(job.finishedAt) }}</span>
                      <span v-if="job.progress?.total">进度：{{ job.progress.current }} / {{ job.progress.total }}</span>
                      <span v-if="job.retryAfterSeconds">建议 {{ job.retryAfterSeconds }} 秒后重试</span>
                      <span v-if="job.error">{{ job.error }}</span>
                    </div>
                  </div>
                  <div class="actions compact">
                    <button v-if="job.retryable || ['failed', 'rate_limited'].includes(job.status)" @click="retryWikiImportJob(job)" :disabled="loading.retryJobId === job.id">
                      {{ loading.retryJobId === job.id ? "重试中..." : "重试" }}
                    </button>
                    <button @click="openWikiImportJob(job)">查看详情</button>
                  </div>
                </article>
              </div>
            </section>

            <div v-if="wikiImportResult" class="import-result" :class="{ failed: ['failed', 'rate_limited'].includes(wikiImportResult.status) }">
              <div class="import-result-head">
                <div>
                  <strong>{{ importStatusText }}</strong>
                  <p v-if="wikiImportResult.error" role="alert" class="node-content-error">{{ wikiImportResult.error }}</p>
                  <p v-if="wikiImportResult.exportDir">{{ wikiImportResult.exportDir }}</p>
                  <p v-if="wikiImportResult.request?.uploadReceipt">最近一次文件选择：{{ wikiImportResult.request.uploadReceipt.chosen }} 个，上传完成 {{ wikiImportResult.request.uploadReceipt.uploaded }} 个，未上传 {{ wikiImportResult.request.uploadReceipt.skipped }} 个<span v-if="wikiImportResult.request.uploadReceipt.skippedFiles.length">（{{ wikiImportResult.request.uploadReceipt.skippedFiles.join('、') }}）</span>。本次任务处理下方已勾选的文件；未勾选文件不参与摄入。</p>
                  <template v-if="wikiImportResult.ingestionReport?.manifestPath">
                    <p>本次处理 {{ wikiImportResult.ingestionReport.totalDocuments ?? wikiImportResult.ingestionReport.documents.length }} 个文件版本：解析失败 {{ wikiImportResult.ingestionReport.conversionFailedDocuments ?? wikiImportResult.ingestionReport.documents.filter(d => d.status === 'conversion_failed').length }}，索引失败 {{ wikiImportResult.ingestionReport.indexFailedDocuments ?? 0 }}，抽取失败 {{ wikiImportResult.ingestionReport.extractionFailedDocuments ?? wikiImportResult.ingestionReport.documents.filter(d => d.status === 'extraction_failed').length }}，发布校验失败 {{ wikiImportResult.ingestionReport.publicationFailedDocuments ?? 0 }}，待完成 {{ wikiImportResult.ingestionReport.pendingDocuments ?? 0 }}。</p>
                    <p v-if="wikiImportResult.ingestionReport.phase === 'published'">已核对实际入库 {{ wikiImportResult.ingestionReport.publishedDocuments }} 个文件版本；项目共 {{ wikiImportResult.ingestionReport.projectDocuments }} 份文档、{{ wikiImportResult.ingestionReport.projectVersions }} 个版本。相同所属文档的多个版本合并显示，非默认版本可在文档版本中查看。</p>
                    <p v-else>抽取就绪 {{ wikiImportResult.ingestionReport.succeededDocuments }} 个文件版本，尚未确认发布入库；以最终发布校验为准。</p>
                    <p>Markdown 留档及逐文件结果：{{ wikiImportResult.ingestionReport.manifestPath }}</p>
                    <details>
                      <summary>查看全部文件的解析、抽取与入库结果（失败 {{ wikiImportResult.ingestionReport.failedDocuments }} 个）</summary>
                      <p v-for="document in wikiImportResult.ingestionReport.documents" :key="document.sourceRef">
                        {{ document.originalSource }}：{{ ({ conversion_failed: '解析失败', index_failed: '索引失败', extraction_failed: '抽取失败', publication_failed: '发布校验失败', converted: '解析完成，待索引', indexed: '索引完成，待抽取', succeeded: wikiImportResult.ingestionReport.phase === 'published' ? '已入库' : '抽取完成，待发布' })[document.status] || document.status }}<span v-if="document.editionId">（{{ document.isDefaultVersion ? '默认版本' : '非默认版本，请在文档版本中查看' }}，{{ document.chunkCount }} 个正文块）</span><span v-if="document.error">：{{ document.error }}</span>
                      </p>
                    </details>
                  </template>
                </div>
                <button @click="clearImportResult">关闭详情</button>
              </div>
              <div v-if="wikiImportResult.progress" class="import-progress">
                <p v-if="wikiImportResult.performance?.conversion?.total">
                  本次原文解析：{{ wikiImportResult.performance.conversion.current }} / {{ wikiImportResult.performance.conversion.total }} 个文件，
                  失败 {{ wikiImportResult.performance.conversion.failed }}，复用解析缓存 {{ wikiImportResult.performance.conversion.cacheHits }}。
                  <span v-if="wikiImportResult.performance.conversion.lastFile">最近处理：{{ wikiImportResult.performance.conversion.lastFile }}</span>
                </p>
                <div>
                  <span>进度</span>
                  <b>{{ wikiImportResult.progress.percent || 0 }}%</b>
                </div>
                <progress :value="wikiImportResult.progress.percent || 0" max="100"></progress>
                <p v-if="wikiImportResult.progress.total">
                  已完成 {{ wikiImportResult.progress.current }} / {{ wikiImportResult.progress.total }} 个标题块
                  <span v-if="wikiImportResult.performance?.failedChunks">；失败 {{ wikiImportResult.performance.failedChunks }} 块，已记录待重试，其余继续处理</span>
                </p>
              </div>
              <div v-if="wikiImportResult.summary" class="import-stats">
                <span>导出 Sources <b>{{ wikiImportResult.summary.sources }}</b></span>
                <span>导出 Objects <b>{{ wikiImportResult.summary.objects || 0 }}</b></span>
                <span>知识对象 <b>{{ wikiImportResult.registeredProject?.searchIndex?.knowledgeObjects || 0 }}</b></span>
                <span>Claims <b>{{ wikiImportResult.registeredProject?.searchIndex?.claims || 0 }}</b></span>
              </div>
              <details v-if="wikiImportLogs" class="import-log" open>
                <summary>查看执行日志</summary>
                <pre>{{ wikiImportLogs }}</pre>
              </details>
            </div>
          </section>

          <section v-else-if="activeAdminPage === 'index'" class="panel">
            <template v-if="currentProject()">
              <div class="panel-head">
                <div>
                  <h2>持久化搜索索引</h2>
                  <span>{{ searchIndexStatus?.ok ? "索引可用" : "需要构建或修复" }}</span>
                </div>
                <div class="actions compact">
                  <button @click="loadSearchIndexStatus" :disabled="loading.searchIndex">检查</button>
                  <button class="primary" @click="rebuildSearchIndex(false)" :disabled="loading.searchIndex">
                    {{ loading.searchIndex ? "构建中..." : "增量构建" }}
                  </button>
                  <button @click="rebuildSearchIndex(true)" :disabled="loading.searchIndex">全量重建</button>
                  <button @click="exportMarkdown" :disabled="loading.searchIndex">导出 Markdown</button>
                  <button @click="toggleIndexInspector" :disabled="loading.indexInspector">
                    {{ indexInspectorOpen ? "关闭文档块" : "检查文档块" }}
                  </button>
                </div>
              </div>
              <div v-if="searchIndexStatus" class="import-stats">
                <span>版本 <b>{{ searchIndexStatus.indexVersion || "-" }}</b></span>
                <span>文档 <b>{{ searchIndexStatus.documents || 0 }}</b></span>
                <span>文档版本 <b>{{ searchIndexStatus.documentVersions || 0 }}</b></span>
                <span>结构节点 <b>{{ searchIndexStatus.structuralNodes || 0 }}</b></span>
                <span>切片 <b>{{ searchIndexStatus.chunks || 0 }}</b></span>
                <span>知识对象 <b>{{ searchIndexStatus.knowledgeObjects || 0 }}</b></span>
                <span>Claim <b>{{ searchIndexStatus.claims || 0 }}</b></span>
                <span>Claim 证据 <b>{{ searchIndexStatus.claimEvidence || 0 }}</b></span>
                <span>对象定位 <b>{{ searchIndexStatus.objectMentions || 0 }}</b></span>
                <span v-if="searchIndexStatus.skipped !== undefined">跳过 <b>{{ searchIndexStatus.skipped }}</b></span>
                <span v-if="searchIndexStatus.indexed !== undefined">更新 <b>{{ searchIndexStatus.indexed }}</b></span>
                <span v-if="(searchIndexStatus.failures || searchIndexStatus.lastAttemptFailures)?.length">
                  失败 <b>{{ (searchIndexStatus.failures || searchIndexStatus.lastAttemptFailures).length }}</b>
                </span>
              </div>
              <p v-if="searchIndexStatus?.reason || searchIndexStatus?.error" class="status">
                {{ searchIndexStatus.reason || searchIndexStatus.error }}
              </p>
              <section v-if="indexInspectorOpen" class="index-inspector">
                <div class="inspector-toolbar">
                  <div>
                    <h3>原文解析与 Chunk 检查</h3>
                    <p>只读查看解析文本、章节结构、切分结果、知识抽取和最终问答证据。</p>
                  </div>
                  <button @click="loadIndexInspectorDocuments" :disabled="loading.indexInspector">刷新</button>
                </div>
                <p v-if="indexInspectorError" class="status">{{ indexInspectorError }}</p>
                <div v-if="loading.indexInspector && !indexInspectorDocuments.length" class="empty compact-empty">正在读取索引内容...</div>
                <div v-else-if="indexInspectorDocuments.length" class="inspector-grid">
                  <aside class="inspector-column inspector-documents">
                    <h4>文档 · {{ indexInspectorDocuments.length }}</h4>
                    <button
                      v-for="document in indexInspectorDocuments"
                      :key="document.id"
                      :class="{ active: inspectorDocument?.document?.id === document.id }"
                      @click="loadInspectorDocument(document.id)"
                    >
                      <strong>{{ document.title }}</strong>
                      <span>{{ document.relative_path }}</span>
                      <small>{{ document.section_count }} 章节 · {{ document.chunk_count }} chunks</small>
                    </button>
                  </aside>

                  <section class="inspector-column inspector-chunks">
                    <template v-if="inspectorDocument?.document">
                      <div class="inspector-column-head">
                        <div>
                          <h4>{{ inspectorDocument.document.title }}</h4>
                          <span>{{ inspectorDocument.document.parsedCharCount }} 字符 · {{ inspectorDocument.chunks.length }} chunks</span>
                        </div>
                        <button v-if="inspectorSectionId" @click="inspectorSectionId = ''">显示全部</button>
                      </div>
                      <details class="inspector-outline" open>
                        <summary>章节结构 · {{ inspectorDocument.sections.length }}</summary>
                        <button
                          v-for="section in inspectorDocument.sections"
                          :key="section.id"
                          :class="{ active: inspectorSectionId === section.id }"
                          :style="{ paddingLeft: `${9 + section.depth * 14}px` }"
                          :title="section.heading_path"
                          @click="inspectorSectionId = section.id"
                        >
                          <span>{{ section.heading || section.heading_path }}</span>
                          <small>{{ section.charCount }} 字符</small>
                        </button>
                      </details>
                      <div class="inspector-chunk-list">
                        <button
                          v-for="chunk in visibleInspectorChunks"
                          :key="chunk.id"
                          :class="{ active: inspectorChunk?.chunk?.id === chunk.id }"
                          @click="loadInspectorChunk(chunk.id)"
                        >
                          <span class="chunk-title">Chunk #{{ chunk.id }} · 文档内第 {{ chunk.chunk_no + 1 }} 块</span>
                          <span>{{ chunk.heading_path || "未识别标题" }}</span>
                          <small>{{ chunk.charCount }} 字符 · {{ chunk.object_count }} 对象 · {{ chunk.claim_count }} Claims</small>
                          <span v-if="chunk.warnings.length" class="chunk-warnings">{{ chunk.warnings.join(" · ") }}</span>
                        </button>
                      </div>
                      <details class="parsed-document">
                        <summary>查看清理后的完整解析文本</summary>
                        <pre>{{ inspectorDocument.document.parsed_text }}</pre>
                      </details>
                    </template>
                    <div v-else class="empty compact-empty">选择左侧文档查看切分结果。</div>
                  </section>

                  <section class="inspector-column inspector-detail">
                    <template v-if="inspectorChunk?.chunk">
                      <div class="inspector-column-head">
                        <div>
                          <h4>Chunk #{{ inspectorChunk.chunk.id }}</h4>
                          <span>第 {{ inspectorChunk.chunk.chunk_no + 1 }} 块 · {{ inspectorChunk.chunk.charCount }} 字符</span>
                        </div>
                        <div class="actions compact">
                          <button :disabled="!inspectorChunk.previousChunkId" @click="loadInspectorChunk(inspectorChunk.previousChunkId)">上一块</button>
                          <button :disabled="!inspectorChunk.nextChunkId" @click="loadInspectorChunk(inspectorChunk.nextChunkId)">下一块</button>
                        </div>
                      </div>
                      <p class="inspector-path">{{ inspectorChunk.chunk.heading_path }}</p>
                      <h5>原始 Chunk</h5>
                      <pre>{{ inspectorChunk.chunk.content }}</pre>
                      <h5>抽取对象 · {{ inspectorChunk.objects.length }}</h5>
                      <div v-if="inspectorChunk.objects.length" class="inspector-extractions">
                        <article v-for="item in inspectorChunk.objects" :key="item.mention_id">
                          <strong>{{ item.name }}</strong><span>{{ item.domain_type }} · 置信度 {{ Number(item.confidence || 0).toFixed(2) }}</span>
                          <p v-if="item.description"><b>对象说明：</b>{{ item.description }}</p>
                          <p v-if="item.aliases"><b>别名：</b>{{ item.aliases }}</p>
                          <p v-if="item.keywords"><b>关键词：</b>{{ item.keywords }}</p>
                          <p v-if="item.matched_text"><b>命中文本：</b>{{ item.matched_text }}</p>
                          <p v-if="item.evidence"><b>原文证据：</b>{{ item.evidence }}</p>
                          <p v-if="item.extraction_summary"><b>提及摘要：</b>{{ item.extraction_summary }}</p>
                        </article>
                      </div>
                      <p v-else class="muted">该块没有对象提及。</p>
                      <h5>Claims · {{ inspectorChunk.claims.length }}</h5>
                      <div v-if="inspectorChunk.claims.length" class="inspector-extractions">
                        <article v-for="claim in inspectorChunk.claims" :key="claim.claim_id">
                          <strong>{{ claim.subject }} {{ claim.predicate }} {{ claim.object }}</strong>
                          <p>{{ claim.quoted_text }}</p>
                        </article>
                      </div>
                      <p v-else class="muted">该块没有 Claim。</p>
                      <details v-if="inspectorChunk.answerEvidence" class="answer-evidence" open>
                        <summary>最终提交给 LLM 的问答证据</summary>
                        <pre>{{ inspectorChunk.answerEvidence.content }}</pre>
                      </details>
                    </template>
                    <div v-else class="empty compact-empty">选择中间的 Chunk 查看完整内容和抽取结果。</div>
                  </section>
                </div>
                <div v-else-if="!indexInspectorError" class="empty compact-empty">索引中还没有可检查的原文文档。</div>
              </section>
            </template>
            <div v-else class="empty">请先在顶部选择一个项目。</div>
          </section>

          <section v-else-if="activeAdminPage === 'synonyms'" class="domain-panel panel">
            <div class="panel-head">
              <h2>同义词库</h2>
              <div class="actions compact">
                <button @click="loadDomainLayer" :disabled="!selectedProjectId || loading.domain">读取</button>
                <button class="primary" @click="saveSynonyms" :disabled="!selectedProjectId || loading.domain">保存 synonym</button>
              </div>
            </div>

            <div class="domain-grid">
              <label class="editor-block">
                <span>synonym.yaml</span>
                <textarea v-model="synonymYaml" class="code-editor" spellcheck="false" />
              </label>
              <label class="editor-block">
                <span>导入同类词</span>
                <textarea v-model="importText" class="code-editor" spellcheck="false" placeholder="撮合:&#10;  - 竞价&#10;  - 成交&#10;  - 连续竞价&#10;  - 集合竞价&#10;&#10;接口: API, HTTP接口" />
                <button @click="importSynonyms" :disabled="!selectedProjectId || !importText.trim() || loading.domain">检测并合并到 synonym</button>
              </label>
            </div>
          </section>

          <section v-else-if="activeAdminPage === 'models'" class="model-config-panel panel">
            <div class="panel-head">
              <div>
                <h2>模型配置</h2>
                <p>管理检索问答和摘要生成可使用的 LLM 接入配置。</p>
              </div>
              <div class="actions compact">
                <button @click="loadLlmModels" :disabled="loading.modelConfig">刷新</button>
              </div>
            </div>

            <div class="model-list">
              <div class="model-list-head">
                <span>显示名 / 模型名</span>
                <span>API Base</span>
                <span>密钥</span>
                <span>操作</span>
              </div>
              <article v-for="model in llmModels" :key="model.id" class="model-row">
                <div>
                  <strong>{{ model.name || model.model }}</strong>
                  <p>{{ model.model }}</p>
                </div>
                <p>{{ model.apiBase || "使用默认 API Base" }}</p>
                <span :class="['key-status', model.hasApiKey ? 'ok' : 'muted']">
                  {{ model.hasApiKey ? "已配置" : "未配置" }}
                </span>
                <div class="actions compact">
                  <span v-if="model.id === 'default'" class="env-model-label">.env 默认项</span>
                  <template v-else>
                    <button @click="editLlmModel(model)" :disabled="loading.modelConfig">编辑</button>
                    <button class="danger" @click="deleteLlmModel(model)" :disabled="loading.modelConfig">删除</button>
                  </template>
                </div>
              </article>
              <div v-if="!llmModels.length" class="empty">还没有模型配置。</div>
            </div>

            <form class="model-editor" @submit.prevent="saveLlmModel">
              <div class="form-section-title">
                <h3>{{ editingModelId ? "编辑模型" : "新增模型" }}</h3>
                <p v-if="editingModelId">API Key 留空时保留原值；也可以勾选清除。</p>
                <p v-else>API Base 和 API Key 留空时使用服务端环境变量。</p>
              </div>
              <div class="model-form">
                <label class="field">
                  <span>显示名</span>
                  <input v-model="modelForm.name" placeholder="例如 DeepSeek V4 Flash" />
                </label>
                <label class="field">
                  <span>模型名</span>
                  <input v-model="modelForm.model" placeholder="例如 deepseek-v4-flash" required />
                </label>
                <label class="field">
                  <span>API Base</span>
                  <input v-model="modelForm.apiBase" placeholder="例如 https://api.example.com/v1" />
                </label>
                <label class="field">
                  <span>API Key</span>
                  <input v-model="modelForm.apiKey" type="password" :placeholder="editingModelId ? '留空则保留原 Key' : '不填则使用 LLM_API_KEY'" />
                </label>
              </div>
              <label v-if="editingModelId && editingModelHasApiKey" class="check-field clear-key-field">
                <input v-model="modelForm.clearApiKey" type="checkbox" />
                <span>清除当前已保存的 API Key</span>
              </label>
              <div class="actions model-editor-actions">
                <button v-if="editingModelId" type="button" @click="resetModelForm" :disabled="loading.modelConfig">取消编辑</button>
                <button class="primary" type="submit" :disabled="!modelForm.model.trim() || loading.modelConfig">
                  {{ loading.modelConfig ? "保存中..." : editingModelId ? "保存修改" : "新增模型" }}
                </button>
              </div>
            </form>
          </section>
        </div>
      </div>
    </section>

    </template>
    <p v-if="!authChecked">正在检查登录状态...</p>
    <div v-if="authChecked && (!currentUser.authenticated || showLoginDialog)" class="login-overlay" @click.self="closeLoginDialog">
      <form class="login-dialog" @submit.prevent="submitLogin">
        <div class="login-head">
          <h2>登录知识库</h2>
          <button v-if="currentUser.authenticated" type="button" class="drawer-close" @click="closeLoginDialog" title="关闭">×</button>
        </div>
        <p class="login-hint">请先登录后使用知识库。普通账户可使用检索问答与关系图谱；管理功能需要管理员账户。</p>
        <label class="field">
          <span>用户名</span>
          <input v-model.trim="loginForm.username" autocomplete="username" autofocus />
        </label>
        <label class="field">
          <span>密码</span>
          <input v-model="loginForm.password" type="password" autocomplete="current-password" />
        </label>
        <p v-if="loginError" class="login-error">{{ loginError }}</p>
        <div class="login-actions">
          <button type="submit" class="primary" :disabled="submittingLogin">
            {{ submittingLogin ? "登录中..." : "登录" }}
          </button>
          <button v-if="currentUser.authenticated" type="button" @click="closeLoginDialog">取消</button>
        </div>
      </form>
    </div>
  </main>
</template>

<script setup>
import { computed, nextTick, onMounted, onUnmounted, ref, watch } from "vue";
import MarkdownBlocks from "./MarkdownBlocks.vue";
import DocumentVersions from "./DocumentVersions.vue";
import ProjectRegistry from "./ProjectRegistry.vue";
import KnowledgeIntake from "./KnowledgeIntake.vue";
import IngestionPromptManager from "./IngestionPromptManager.vue";
import DocumentVersionLibrary from "./DocumentVersionLibrary.vue";
import EvaluationPanel from "./EvaluationPanel.vue";
import { evidenceSelectionKey, selectedEvidencePayload, uniqueEvidenceResults } from "./evidenceSelection.js";

const activeView = ref("qa");
const accountMenuOpen = ref(false);
const projectMenuOpen = ref(false);
const searchSettingsOpen = ref(false);
const timelineOpen = ref(false);
const projects = ref([]);
const selectedProjectId = ref(localStorage.getItem("selectedProjectId") || "");
const projectRefreshMinutes = ref(0);
const projectRefreshTimer = ref(null);
const query = ref("");
const mode = ref("hybrid");
const limit = ref(8);
const llmModels = ref([]);
const selectedModelId = ref(localStorage.getItem("selectedModelId") || "default");
const useSynonyms = ref(true);
const useIntent = ref(true);
const contextMaxChars = ref(Number(localStorage.getItem("contextMaxChars")) || 24000);
const contextReserveChars = ref(Number(localStorage.getItem("contextReserveChars") ?? 2000));
watch(contextMaxChars, value => localStorage.setItem("contextMaxChars", String(value)));
watch(contextReserveChars, value => localStorage.setItem("contextReserveChars", String(value)));
const rerankWithLlm = ref(false);
const rerankThreshold = ref(0.45);
const supplementRawSearch = ref(localStorage.getItem("supplementRawSearch") === "true");
const results = ref([]);
const selectedEvidenceKeys = ref([]);
const answer = ref("");
const thinking = ref("");
const prompt = ref("");
const answerMeta = ref("");
const answerPanelEl = ref(null);
const answerEvidenceDocs = ref([]);
const flashingEvidenceKey = ref("");
const evidenceEls = new Map();
let citeFlashTimer = null;
const evidenceDocItem = ref(null);
const evidenceDocContent = ref("");
const evidenceDocError = ref("");
const composeStage = ref("");
const status = ref("");
const synonymYaml = ref("");
const importText = ref("");
const emptyModelForm = () => ({ name: "", model: "", apiBase: "", apiKey: "", clearApiKey: false });
const modelForm = ref(emptyModelForm());
const editingModelId = ref("");
const editingModelHasApiKey = ref(false);
const modelTestResult = ref(null);
const synonymExpansion = ref({ expandedTerms: [], matchedGroups: [] });
const queryIntent = ref({ enabled: false, intent: "general", focusTerms: [], searchTerms: [], reason: "" });
const lastSearchQuery = ref("");
const lastSearchedQuestion = ref("");
const searchContextId = ref("");
const versionScope = ref({});
const versionRefreshKey = ref(0);
const knowledgeIntakeRef = ref(null);
const preferredDocumentId = ref('');
watch(versionScope, () => {
  results.value = [];
  selectedEvidenceKeys.value = [];
  searchContextId.value = "";
  answer.value = "";
  thinking.value = "";
  prompt.value = "";
  answerEvidenceDocs.value = [];
  closeEvidenceDoc();
  graphData.value = null;
  if (activeView.value === "graph") loadGraph();
});
const llmRerank = ref({ enabled: false, reason: "" });
const searchRuntimeIndex = ref(null);
const highlightStopTerms = new Set([
  "各自",
  "分别",
  "区别",
  "原则",
  "规则",
  "要求",
  "条件",
  "流程",
  "方式",
  "内容",
  "什么",
  "哪些",
  "几种",
  "几类",
  "是什么",
  "有什么",
  "有哪些"
]);
const wikiImport = ref({
  projectName: "",
  projectDir: "",
  rawDir: "",
  exportDir: "",
  limitDocs: 0,
  sourceFiles: "",
  llmConcurrency: 4,
  promptProfile: 'compact',
  extractionBatchChars: 3000,
  extractionBatchSize: 6,
  allowHeuristic: false
});
const wikiImportResult = ref(null);
const wikiImportJobId = ref(localStorage.getItem("wikiImportJobId") || "");
const wikiImportActiveJobIds = ref(readStoredWikiImportJobIds());
const wikiImportJobs = ref([]);
const wikiImportJobTotal = ref(0);
const registeredProjects = ref([]);
const searchIndexStatus = ref(null);
const indexInspectorOpen = ref(false);
const indexInspectorDocuments = ref([]);
const inspectorDocument = ref(null);
const inspectorChunk = ref(null);
const inspectorSectionId = ref("");
const indexInspectorError = ref("");
let wikiImportPollTimer = null;
let wikiImportPolling = false;
const graphData = ref(null);
const graphError = ref("");
const graphMode = ref(localStorage.getItem("graphMode") === "structure" ? "structure" : "relations");
const selectedGraphNode = ref(null);
const nodeContentCache = ref({});
const nodeContentError = ref("");
const selectedGraphTypes = ref([]);
const graphView = ref({ x: 0, y: 0, scale: 1 });
const graphNodePositions = ref({});
const graphDrag = ref(null);
const collapsedGraphNodeGroups = ref({});
const loading = ref({ projects: false, search: false, compose: false, domain: false, graph: false, nodeContent: false, evidenceDoc: false, importLayer: false, importSubmit: false, importJobs: false, retryJobId: "", modelTest: false, modelConfig: false, searchIndex: false, indexInspector: false });

const currentUser = ref({ authenticated: false, username: "", isAdmin: false });
const authChecked = ref(false);
const loginForm = ref({ username: "", password: "" });
const showLoginDialog = ref(false);
const loginError = ref("");
const submittingLogin = ref(false);

watch(selectedProjectId, value => {
  if (value) localStorage.setItem("selectedProjectId", value);
  else localStorage.removeItem("selectedProjectId");
});

watch(selectedModelId, value => {
  if (value) localStorage.setItem("selectedModelId", value);
  else localStorage.removeItem("selectedModelId");
});

watch(supplementRawSearch, value => {
  localStorage.setItem("supplementRawSearch", value ? "true" : "false");
});

watch(graphMode, value => {
  localStorage.setItem("graphMode", value);
  selectedGraphNode.value = null;
  selectedGraphTypes.value = [];
  resetGraphView();
});

const canQuery = computed(() => selectedProjectId.value && query.value.trim());

const exampleQuestions = [
  "登录接口鉴权失败时应该检查哪些配置？",
  "集合竞价和连续竞价有什么区别？",
  "异常交易的判定标准是什么？"
];

function askExample(question) {
  query.value = question;
  if (canQuery.value) searchAndComposeTopThree();
}
const activeWikiImportJobCount = computed(() => wikiImportActiveJobIds.value.length);

/* ===== 信息架构：空间 / 子页导航 ===== */
const adminNavItems = [
  { key: "admin-evaluation", label: "问答评测" },
  { key: "admin-projects", label: "项目管理" },
  { key: "admin-ingestion", label: "知识摄入" },
  { key: "admin-prompts", label: "提示词管理" },
  { key: "admin-versions", label: "文档版本管理" },
  { key: "admin-index", label: "搜索索引" },
  { key: "admin-synonyms", label: "同义词库" },
  { key: "admin-models", label: "模型配置" }
];
const lastQueryView = ref("qa");
const lastAdminPage = ref("admin-projects");
const activeSpace = computed(() => (activeView.value.startsWith("admin-") ? "admin" : "query"));
const activeAdminPage = computed(() => (activeSpace.value === "admin" ? activeView.value.replace(/^admin-/, "") : ""));
const visibleInspectorChunks = computed(() => {
  const chunks = inspectorDocument.value?.chunks || [];
  if (!inspectorSectionId.value) return chunks;
  const section = inspectorDocument.value?.sections?.find(item => item.id === inspectorSectionId.value);
  return section ? chunks.filter(chunk => String(chunk.heading_path || "").startsWith(section.heading_path)) : chunks;
});
const searchSettingsEnabledCount = computed(() =>
  [useSynonyms, useIntent, rerankWithLlm, supplementRawSearch].filter(flag => flag.value).length
);
const graphViewBox = computed(() => {
  const nodes = graphModel.value.nodes;
  if (!nodes.length) return "-560 -390 1120 780";
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const node of nodes) {
    const labelExtent = node.showLabel ? estimateLabelWidth(node.label) + 8 : 0;
    minX = Math.min(minX, node.x - node.radius - 4);
    maxX = Math.max(maxX, node.x + node.radius + labelExtent + 4);
    minY = Math.min(minY, node.y - node.radius - 4);
    maxY = Math.max(maxY, node.y + node.radius + 4);
  }
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  const width = Math.max(maxX - minX + 60, 240);
  const height = Math.max(maxY - minY + 60, 240);
  return `${cx - width / 2} ${cy - height / 2} ${width} ${height}`;
});
const graphTransform = computed(() => `translate(${graphView.value.x} ${graphView.value.y}) scale(${graphView.value.scale})`);
const graphModel = computed(() => {
  const model = selectGraphMode(normalizeGraph(graphData.value), graphMode.value);
  return graphMode.value === "structure"
    ? layoutStructureTree(model, graphNodePositions.value)
    : layoutGraph(model, graphNodePositions.value);
});
const graphTypeCounts = computed(() => {
  const counts = {};
  for (const node of graphModel.value.nodes) counts[node.type] = (counts[node.type] || 0) + 1;
  return counts;
});
const graphTypes = computed(() => {
  const order = ["Overview", "Object", "Section", "Source"];
  return Object.keys(graphTypeCounts.value).sort((a, b) => {
    const ia = order.includes(a) ? order.indexOf(a) : order.length;
    const ib = order.includes(b) ? order.indexOf(b) : order.length;
    return ia - ib || a.localeCompare(b);
  });
});
const graphSummary = computed(() => {
  if (loading.value.graph) return "正在同步 /api/v1/projects/{id}/graph";
  if (graphError.value) return "图谱接口暂不可用";
  if (!graphModel.value.nodes.length) return "从本地知识图谱读取对象、来源和页面连接。";
  const label = graphMode.value === "structure" ? "章节证据图" : "原关系图";
  return `${label} · ${graphModel.value.nodes.length} 个节点，${graphModel.value.links.length} 条连接`;
});
const visibleGraph = computed(() => {
  const activeTypes = selectedGraphTypes.value.length ? new Set(selectedGraphTypes.value) : null;
  const nodes = graphModel.value.nodes.filter(node => !activeTypes || activeTypes.has(node.type));
  const nodeIds = new Set(nodes.map(node => node.id));
  const links = graphModel.value.links.filter(link => nodeIds.has(link.source) && nodeIds.has(link.target));
  return { nodes, links };
});
const graphNodeGroups = computed(() => {
  const groups = new Map();
  const nodes = [...visibleGraph.value.nodes].sort((a, b) => graphNodeSortLabel(a).localeCompare(graphNodeSortLabel(b), "zh-Hans-u-co-pinyin"));
  for (const node of nodes) {
    const key = graphNodeInitial(node.label);
    if (!groups.has(key)) groups.set(key, []);
    groups.get(key).push(node);
  }
  return [...groups.entries()]
    .sort(([a], [b]) => graphGroupSortValue(a).localeCompare(graphGroupSortValue(b)))
    .map(([key, nodes]) => ({ key, nodes }));
});
const showRerankStrip = computed(() => {
  if (llmRerank.value?.enabled) return true;
  const reason = llmRerank.value?.reason || "";
  return Boolean(reason);
});
const queryIntentTerms = computed(() => uniqueTerms([...(queryIntent.value?.focusTerms || []), ...(queryIntent.value?.searchTerms || [])]).slice(0, 10));
const modelTestResultText = computed(() => {
  const result = modelTestResult.value;
  if (!result) return "";
  const elapsed = Number.isFinite(Number(result.elapsedMs)) ? `${result.elapsedMs}ms` : "-";
  const prefix = result.ok ? "可用" : "不可用";
  const sample = result.sample ? ` · ${result.sample}` : "";
  return `${prefix} · ${elapsed}${sample}`;
});
const showIntentStrip = computed(() => {
  if (!queryIntent.value) return false;
  return Boolean(queryIntent.value.enabled || queryIntentTerms.value.length || queryIntent.value.reason);
});
const showSearchTimeline = computed(() => Boolean(
  synonymExpansion.value.expandedTerms?.length ||
  showIntentStrip.value ||
  showRerankStrip.value ||
  searchRuntimeIndex.value?.ok
));
const searchTimelineSummary = computed(() => {
  const parts = [];
  if (showIntentStrip.value) parts.push(`意图: ${queryIntent.value.intent || "general"}`);
  if (synonymExpansion.value.expandedTerms?.length) {
    parts.push(`扩展 ${synonymExpansion.value.expandedTerms.length} 词`);
  }
  if (searchRuntimeIndex.value?.ok) {
    const retrievalMode = searchRuntimeIndex.value.retrievalMode;
    const modeText = retrievalMode === "object-linked"
      ? "对象直连原文"
      : retrievalMode === "object-source-local-fts"
        ? "关联原文局部 FTS"
        : "全项目 FTS";
    const elapsed = searchRuntimeIndex.value.elapsedMs !== undefined ? ` · ${searchRuntimeIndex.value.elapsedMs}ms` : "";
    parts.push(`${modeText}${elapsed}`);
  }
  if (llmRerank.value?.enabled) parts.push("LLM 已筛选");
  return parts.join(" · ") || "查看检索细节";
});

watch(results, () => {
  timelineOpen.value = false;
});
const selectedNodeContentBlocks = computed(() => renderMarkdownBlocks(selectedNodeContent(selectedGraphNode.value), { links: true }));
const renderedAnswerBlocks = computed(() => renderMarkdownBlocks(answer.value));
const wikiImportLogs = computed(() => {
  const logs = wikiImportResult.value?.logs;
  if (Array.isArray(logs)) return logs.join("\n");
  return [wikiImportResult.value?.stdout, wikiImportResult.value?.stderr].filter(Boolean).join("\n");
});
const importStatusText = computed(() => {
  const statusValue = wikiImportResult.value?.status;
  if (statusValue === "running" && (wikiImportResult.value?.progress?.percent || 0) >= 100) return "抽取完成，正在提交 V7 内容索引";
  if (statusValue === "running") return "生成中";
  if (statusValue === "succeeded") return "生成完成";
  if (statusValue === "partial_succeeded") return "部分成功，失败文档已跳过，可重试";
  if (statusValue === "failed") return "生成失败";
  return wikiImportResult.value?.ok ? "生成完成" : "生成失败";
});

async function api(path, options = {}) {
  const response = await fetch(path, {
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options
  });
  const contentType = response.headers.get("content-type") || "";
  const data = contentType.includes("application/json")
    ? await response.json()
    : { error: `接口没有返回 JSON，可能是后端服务未启动或 /api 代理未命中：${path}`, detail: await response.text() };
  if (!response.ok) {
    if (response.status === 401 && data.needLogin) requireLogin();
    const err = new Error(data.error || data.detail || "请求失败");
    err.status = response.status;
    err.payload = data;
    throw err;
  }
  if (!contentType.includes("application/json")) {
    const err = new Error(data.error);
    err.payload = data;
    throw err;
  }
  return data;
}

function projectPath(path) {
  return `/api/projects/${encodeURIComponent(selectedProjectId.value)}${path}`;
}

function streamProjectPath(path) {
  return projectPath(path);
}

function projectRefreshStorageKey(projectId = selectedProjectId.value) {
  return `projectRefreshMinutes:${projectId || "none"}`;
}

function loadProjectRefreshInterval() {
  projectRefreshMinutes.value = Number(localStorage.getItem(projectRefreshStorageKey()) || 0);
  resetProjectRefreshTimer();
}

function handleProjectRefreshIntervalChange() {
  localStorage.setItem(projectRefreshStorageKey(), String(Number(projectRefreshMinutes.value) || 0));
  resetProjectRefreshTimer();
}

function resetProjectRefreshTimer() {
  stopProjectRefreshTimer();
  const minutes = Number(projectRefreshMinutes.value) || 0;
  if (!minutes || !selectedProjectId.value) return;
  projectRefreshTimer.value = window.setInterval(() => {
    refreshCurrentProject({ silent: true });
  }, minutes * 60 * 1000);
}

function stopProjectRefreshTimer() {
  if (projectRefreshTimer.value) {
    window.clearInterval(projectRefreshTimer.value);
    projectRefreshTimer.value = null;
  }
}

function clearProjectViewCache() {
  graphData.value = null;
  graphError.value = "";
  selectedGraphNode.value = null;
  nodeContentCache.value = {};
  nodeContentError.value = "";
  graphNodePositions.value = {};
  collapsedGraphNodeGroups.value = {};
}

async function refreshCurrentProject(options = {}) {
  if (!selectedProjectId.value) return;
  const currentId = selectedProjectId.value;
  clearProjectViewCache();
  await loadProjects({ preserveSelection: true, silent: true });
  if (currentId && projects.value.some(project => (project.id || project.path) === currentId)) {
    selectedProjectId.value = currentId;
  }
  await loadDomainLayer();
  if (activeView.value === "graph") {
    await loadGraph();
  }
  if (activeView.value === "qa" && query.value.trim()) {
    await runSearch({ preserveAnswer: true });
  }
  if (!options.silent) {
    status.value = `已刷新当前项目：${currentProject()?.name || currentProject()?.title || selectedProjectId.value}`;
  }
}

async function loadCurrentUser() {
  try {
    const data = await api("/api/auth/me/");
    currentUser.value = {
      authenticated: !!data.authenticated,
      username: data.username || "",
      isAdmin: !!data.isAdmin
    };
  } catch (error) {
    currentUser.value = { authenticated: false, username: "", isAdmin: false };
  } finally {
    authChecked.value = true;
  }
}

async function loadLlmModels() {
  try {
    const data = await api("/api/llm-models/");
    llmModels.value = data.models || [];
    if (!llmModels.value.some(model => model.id === selectedModelId.value)) {
      selectedModelId.value = llmModels.value[0]?.id || "default";
    }
  } catch (error) {
    status.value = error.message;
  }
}

async function saveLlmModel() {
  loading.value.modelConfig = true;
  try {
    const data = await api("/api/llm-models/", {
      method: editingModelId.value ? "PUT" : "POST",
      body: JSON.stringify({
        id: editingModelId.value || undefined,
        name: modelForm.value.name.trim(),
        model: modelForm.value.model.trim(),
        apiBase: modelForm.value.apiBase.trim(),
        apiKey: modelForm.value.apiKey,
        clearApiKey: !!modelForm.value.clearApiKey
      })
    });
    llmModels.value = data.models || [];
    selectedModelId.value = data.model?.id || selectedModelId.value;
    const wasEditing = !!editingModelId.value;
    resetModelForm();
    status.value = wasEditing ? "模型配置已更新" : "模型配置已新增";
  } catch (error) {
    if (handleAdminRequired(error)) return;
    status.value = error.message;
  } finally {
    loading.value.modelConfig = false;
  }
}

function editLlmModel(model) {
  editingModelId.value = model.id;
  editingModelHasApiKey.value = !!model.hasApiKey;
  modelForm.value = {
    name: model.name || "",
    model: model.model || "",
    apiBase: model.apiBase || "",
    apiKey: "",
    clearApiKey: false
  };
}

function resetModelForm() {
  editingModelId.value = "";
  editingModelHasApiKey.value = false;
  modelForm.value = emptyModelForm();
}

async function deleteLlmModel(model) {
  if (!window.confirm(`确定删除模型配置“${model.name || model.model}”吗？`)) return;
  loading.value.modelConfig = true;
  try {
    const data = await api("/api/llm-models/", {
      method: "DELETE",
      body: JSON.stringify({ id: model.id })
    });
    llmModels.value = data.models || [];
    if (selectedModelId.value === model.id) {
      selectedModelId.value = llmModels.value[0]?.id || "default";
    }
    if (editingModelId.value === model.id) resetModelForm();
    status.value = "模型配置已删除";
  } catch (error) {
    if (handleAdminRequired(error)) return;
    status.value = error.message;
  } finally {
    loading.value.modelConfig = false;
  }
}

async function testSelectedModel() {
  loading.value.modelTest = true;
  modelTestResult.value = null;
  try {
    const data = await api("/api/llm-models/test/", {
      method: "POST",
      body: JSON.stringify({ modelId: selectedModelId.value })
    });
    modelTestResult.value = data;
    status.value = data.ok
      ? `当前模型可用，响应时间 ${data.elapsedMs}ms`
      : `当前模型不可用：${data.message || "未知错误"}`;
  } catch (error) {
    modelTestResult.value = { ok: false, elapsedMs: 0, message: error.message };
    status.value = error.message;
  } finally {
    loading.value.modelTest = false;
  }
}

async function submitLogin() {
  if (!loginForm.value.username.trim() || !loginForm.value.password) {
    loginError.value = "请输入用户名和密码";
    return;
  }
  submittingLogin.value = true;
  loginError.value = "";
  try {
    const data = await api("/api/auth/login/", {
      method: "POST",
      body: JSON.stringify({
        username: loginForm.value.username.trim(),
        password: loginForm.value.password
      })
    });
    currentUser.value = {
      authenticated: true,
      username: data.username || loginForm.value.username.trim(),
      isAdmin: !!data.isAdmin
    };
    showLoginDialog.value = false;
    loginForm.value = { username: "", password: "" };
    status.value = `已登录：${currentUser.value.username}${currentUser.value.isAdmin ? "（管理员）" : ""}`;
    if (!currentUser.value.isAdmin && activeView.value.startsWith("admin-")) {
      activeView.value = "qa";
    }
    window.location.reload();
  } catch (error) {
    loginError.value = error.message || "登录失败";
  } finally {
    submittingLogin.value = false;
  }
}

async function submitLogout() {
  try {
    await api("/api/auth/logout/", { method: "POST" });
  } catch (error) {
    status.value = error.message || "退出登录失败，请重试";
    return;
  }
  requireLogin();
  window.location.reload();
}

function requireLogin() {
  currentUser.value = { authenticated: false, username: "", isAdmin: false };
  stopWikiImportPolling();
  stopProjectRefreshTimer();
  accountMenuOpen.value = false;
  loginError.value = "登录已失效，请重新登录";
  showLoginDialog.value = true;
}

function openLoginDialog() {
  loginError.value = "";
  showLoginDialog.value = true;
}

function closeLoginDialog() {
  if (!currentUser.value.authenticated) return;
  showLoginDialog.value = false;
  loginError.value = "";
}

function handleAdminRequired(error) {
  if (error?.payload?.needAdmin || /需要管理员/.test(error?.message || "")) {
    if (!currentUser.value.authenticated) {
      openLoginDialog();
    } else {
      status.value = "当前账户不是管理员，请使用管理员账户登录";
    }
    return true;
  }
  return false;
}

function asProjectList(payload) {
  if (Array.isArray(payload)) return payload;
  return payload.projects || payload.items || [];
}

function currentProject() {
  return projects.value.find(project => (project.id || project.path) === selectedProjectId.value) || null;
}

function currentProjectPath() {
  const project = currentProject();
  return project?.path || "";
}

function fillImportFromProject() {
  const path = currentProjectPath();
  if (path) {
    wikiImport.value.projectDir = path;
    if (!wikiImport.value.projectName.trim()) {
      wikiImport.value.projectName = currentProject()?.name || currentProject()?.title || pathName(path);
    }
    wikiImport.value.rawDir = "";
    wikiImport.value.exportDir = "";
  } else {
    status.value = "当前项目列表没有返回 path，请手动填写项目目录";
  }
}

function pathName(path) {
  const clean = String(path || "").replace(/\\/g, "/").replace(/\/$/, "");
  return clean.split("/").pop() || clean;
}

async function loadProjects(options = {}) {
  loading.value.projects = true;
  if (!options.silent) status.value = "";
  try {
    const data = await api("/api/projects/");
    projects.value = asProjectList(data);
    registeredProjects.value = projects.value;
    const projectIds = projects.value.map(project => project.id || project.path).filter(Boolean);
    const savedProjectId = localStorage.getItem("selectedProjectId") || "";
    if (!projects.value.length) { selectedProjectId.value = ''; versionScope.value = {}; }
    if (projects.value.length && (!options.preserveSelection || !projectIds.includes(selectedProjectId.value))) {
      const nextProjectId = projectIds.includes(selectedProjectId.value)
        ? selectedProjectId.value
        : projectIds.includes(savedProjectId)
          ? savedProjectId
          : projectIds[0];
      selectedProjectId.value = nextProjectId || "";
      fillImportFromProject();
      await loadDomainLayer();
      await loadSearchIndexStatus();
    }
    if (options.silent) {
      return;
    }
    status.value = `已加载 ${projects.value.length} 个项目`;
  } catch (error) {
    status.value = error.message;
  } finally {
    loading.value.projects = false;
  }
}

async function loadRegisteredProjects() {
  try {
    const data = await api("/api/projects/");
    registeredProjects.value = data.projects || [];
  } catch (error) {
    status.value = error.message;
  }
}

async function selectRegisteredProject(project) {
  selectedProjectId.value = project.id;
  wikiImport.value.projectName = project.name || "";
  wikiImport.value.projectDir = project.path || "";
  await handleProjectChange();
  status.value = `已选择项目：${project.name}`;
}

async function removeRegisteredProject(project) {
  try {
    await api("/api/projects/", {
      method: "DELETE",
      body: JSON.stringify({ id: project.id })
    });
    if (selectedProjectId.value === project.id) selectedProjectId.value = "";
    await loadProjects();
    status.value = "已移除项目登记，磁盘文件未删除";
  } catch (error) {
    if (handleAdminRequired(error)) return;
    status.value = error.message;
  }
}

async function handleProjectChange() {
  versionScope.value = {};
  preferredDocumentId.value = '';
  clearProjectViewCache();
  results.value = [];
  selectedEvidenceKeys.value = [];
  searchContextId.value = "";
  answer.value = "";
  thinking.value = "";
  prompt.value = "";
  answerEvidenceDocs.value = [];
  indexInspectorDocuments.value = [];
  inspectorDocument.value = null;
  inspectorChunk.value = null;
  inspectorSectionId.value = "";
  closeEvidenceDoc();
  fillImportFromProject();
  loadProjectRefreshInterval();
  await loadDomainLayer();
  await loadSearchIndexStatus();
  if (activeView.value === "graph") {
    await loadGraph();
  }
}

async function loadSearchIndexStatus() {
  if (!selectedProjectId.value) {
    searchIndexStatus.value = null;
    return;
  }
  loading.value.searchIndex = true;
  try {
    searchIndexStatus.value = await api(projectPath("/search-index/"));
  } catch (error) {
    searchIndexStatus.value = { ok: false, error: error.message };
  } finally {
    loading.value.searchIndex = false;
  }
}

async function toggleIndexInspector() {
  indexInspectorOpen.value = !indexInspectorOpen.value;
  if (indexInspectorOpen.value && !indexInspectorDocuments.value.length) {
    await loadIndexInspectorDocuments();
  }
}

async function loadIndexInspectorDocuments() {
  if (!selectedProjectId.value) return;
  loading.value.indexInspector = true;
  indexInspectorError.value = "";
  try {
    const data = await api(projectPath("/index-inspector/"));
    if (!data.ok) throw new Error(data.reason || "无法读取索引内容");
    indexInspectorDocuments.value = data.documents || [];
    const selectedId = inspectorDocument.value?.document?.id;
    const nextId = indexInspectorDocuments.value.some(item => item.id === selectedId)
      ? selectedId
      : indexInspectorDocuments.value[0]?.id;
    if (nextId) await loadInspectorDocument(nextId);
  } catch (error) {
    if (!handleAdminRequired(error)) indexInspectorError.value = error.message;
  } finally {
    loading.value.indexInspector = false;
  }
}

async function loadInspectorDocument(documentId) {
  if (!documentId) return;
  loading.value.indexInspector = true;
  indexInspectorError.value = "";
  inspectorChunk.value = null;
  inspectorSectionId.value = "";
  try {
    const data = await api(projectPath(`/index-inspector/?documentId=${encodeURIComponent(documentId)}`));
    if (!data.ok) throw new Error(data.reason || "无法读取文档切片");
    inspectorDocument.value = data;
    if (data.chunks?.length) await loadInspectorChunk(data.chunks[0].id);
  } catch (error) {
    if (!handleAdminRequired(error)) indexInspectorError.value = error.message;
  } finally {
    loading.value.indexInspector = false;
  }
}

async function loadInspectorChunk(chunkId) {
  if (!chunkId) return;
  loading.value.indexInspector = true;
  indexInspectorError.value = "";
  try {
    const data = await api(projectPath(`/index-inspector/?chunkId=${encodeURIComponent(chunkId)}`));
    if (!data.ok) throw new Error(data.reason || "无法读取 Chunk 详情");
    inspectorChunk.value = data;
  } catch (error) {
    if (!handleAdminRequired(error)) indexInspectorError.value = error.message;
  } finally {
    loading.value.indexInspector = false;
  }
}

async function rebuildSearchIndex(force = false) {
  if (!selectedProjectId.value) return;
  loading.value.searchIndex = true;
  status.value = force ? "正在全量重建搜索索引..." : "正在增量更新搜索索引...";
  try {
    searchIndexStatus.value = await api(projectPath("/search-index/"), {
      method: "POST",
      body: JSON.stringify({ force })
    });
    status.value = searchIndexStatus.value.ok ? "搜索索引构建完成" : "搜索索引构建完成，但存在失败文件";
    if (indexInspectorOpen.value) await loadIndexInspectorDocuments();
  } catch (error) {
    if (handleAdminRequired(error)) return;
    searchIndexStatus.value = { ok: false, error: error.message };
    status.value = error.message;
  } finally {
    loading.value.searchIndex = false;
  }
}

async function exportMarkdown() {
  if (!selectedProjectId.value) return;
  loading.value.searchIndex = true;
  status.value = "正在从 V7 内容库导出 Markdown...";
  try {
    const data = await api(projectPath("/export/"), { method: "POST" });
    status.value = `Markdown 已导出到 ${data.export?.path || "项目导出目录"}`;
  } catch (error) {
    if (!handleAdminRequired(error)) status.value = error.message;
  } finally {
    loading.value.searchIndex = false;
  }
}

function switchSpace(space) {
  if (space === activeSpace.value) return;
  switchView(space === "admin" ? lastAdminPage.value : lastQueryView.value);
}

async function switchView(view) {
  if (view.startsWith("admin-") && !currentUser.value.isAdmin) {
    status.value = currentUser.value.authenticated
      ? "当前账户不是管理员，无法使用该管理功能"
      : "该功能仅管理员可用，请先登录管理员账户";
    openLoginDialog();
    return;
  }
  activeView.value = view;
  if (view.startsWith("admin-")) {
    lastAdminPage.value = view;
  } else {
    lastQueryView.value = view;
  }
  if (view === "graph" && selectedProjectId.value && !graphData.value && !loading.value.graph) {
    await loadGraph();
  }
  if (view === "admin-projects") {
    await loadRegisteredProjects();
  }
  if (view === "admin-ingestion") {
    await loadWikiImportJobs();
  }
  if (view === "admin-index") {
    await loadSearchIndexStatus();
  }
  if (view === "admin-models") {
    await loadLlmModels();
  }
}

async function loadDomainLayer() {
  if (!selectedProjectId.value) return;
  loading.value.domain = true;
  try {
    const synonymsData = await api(projectPath("/domain-layer/synonyms/"));
    synonymYaml.value = synonymsData.yaml || "";
  } catch (error) {
    status.value = error.message;
  } finally {
    loading.value.domain = false;
  }
}

async function loadGraph() {
  if (!selectedProjectId.value) return;
  loading.value.graph = true;
  graphError.value = "";
  selectedGraphNode.value = null;
  nodeContentCache.value = {};
  nodeContentError.value = "";
  try {
    graphData.value = await api(projectPath(`/graph/?versionScope=${encodeURIComponent(JSON.stringify(versionScope.value))}`));
    selectedGraphTypes.value = [];
    graphNodePositions.value = {};
    collapsedGraphNodeGroups.value = {};
    resetGraphView();
  } catch (error) {
    graphData.value = null;
    graphError.value = error.message;
  } finally {
    loading.value.graph = false;
  }
}

function versionsChanged() {
  versionRefreshKey.value++;
  versionScope.value = { ...versionScope.value };
}
function openVersionIntake(group) {
  preferredDocumentId.value = group.id;
  activeView.value = 'admin-ingestion';
}
async function buildWikiLayer() {
  loading.value.importSubmit = true;
  wikiImportResult.value = null;
  status.value = "正在提交知识摄入任务...";
  try {
    const plan = knowledgeIntakeRef.value?.getPlan();
    if (!plan) throw new Error('请先添加要摄入的文件');
    const payload = {
      ...plan,
      limitDocs: 0,
      llmConcurrency: Math.max(1, Number(wikiImport.value.llmConcurrency) || 4),
      promptProfile: wikiImport.value.promptProfile,
      extractionBatchChars: Number(wikiImport.value.extractionBatchChars) || 3000,
      extractionBatchSize: Number(wikiImport.value.extractionBatchSize) || 6,
      modelId: selectedModelId.value,
      allowHeuristic: wikiImport.value.allowHeuristic,
      projectName: plan.projectName
    };
    const data = await api("/api/ingestion/build/", {
      method: "POST",
      body: JSON.stringify(payload)
    });
    wikiImportResult.value = data;
    wikiImportJobId.value = data.id || "";
    if (wikiImportJobId.value) {
      localStorage.setItem("wikiImportJobId", wikiImportJobId.value);
      addActiveWikiImportJob(wikiImportJobId.value);
      updateWikiImportJobInHistory(data);
      wikiImportJobTotal.value += 1;
      loading.value.importLayer = true;
      startWikiImportPolling();
    }
    status.value = `知识摄入任务已启动，当前 ${activeWikiImportJobCount.value} 个任务并行运行`;
  } catch (error) {
    if (handleAdminRequired(error)) {
      return;
    }
    wikiImportResult.value = { ok: false, status: "failed", error: error.message };
    status.value = error.message;
  } finally {
    loading.value.importSubmit = false;
  }
}

async function registerExistingWikiLayer() {
  loading.value.importSubmit = true;
  wikiImportResult.value = null;
  status.value = "正在登记原文项目...";
  try {
    const data = await api("/api/projects/register/", {
      method: "POST",
      body: JSON.stringify({
        projectDir: wikiImport.value.projectDir.trim(),
        rawDir: wikiImport.value.rawDir.trim(),
        exportDir: wikiImport.value.exportDir.trim(),
        projectName: wikiImport.value.projectName.trim() || pathName(wikiImport.value.projectDir)
      })
    });
    wikiImportResult.value = {
      ok: true,
      status: "succeeded",
      projectDir: data.projectDir,
      exportDir: data.exportDir,
      summary: data.summary,
      logs: ["已登记原文项目并建立 V7 索引。"],
      registeredProject: data.registeredProject
    };
    await loadProjects();
    if (data.registeredProject?.id) selectedProjectId.value = data.registeredProject.id;
    status.value = "原文项目已加入项目列表";
  } catch (error) {
    if (handleAdminRequired(error)) {
      return;
    }
    wikiImportResult.value = { ok: false, status: "failed", error: error.message, logs: [error.message] };
    status.value = error.message;
  } finally {
    loading.value.importSubmit = false;
  }
}

function readStoredWikiImportJobIds() {
  try {
    const values = JSON.parse(localStorage.getItem("wikiImportActiveJobIds") || "[]");
    if (Array.isArray(values)) return [...new Set(values.filter(Boolean))];
  } catch {
    // Ignore legacy or damaged local storage and rebuild it from server history.
  }
  const legacyId = localStorage.getItem("wikiImportJobId");
  return legacyId ? [legacyId] : [];
}

function persistActiveWikiImportJobs() {
  localStorage.setItem("wikiImportActiveJobIds", JSON.stringify(wikiImportActiveJobIds.value));
}

function addActiveWikiImportJob(jobId) {
  if (!jobId || wikiImportActiveJobIds.value.includes(jobId)) return;
  wikiImportActiveJobIds.value = [...wikiImportActiveJobIds.value, jobId];
  persistActiveWikiImportJobs();
}

function removeActiveWikiImportJob(jobId) {
  wikiImportActiveJobIds.value = wikiImportActiveJobIds.value.filter(id => id !== jobId);
  persistActiveWikiImportJobs();
}

function startWikiImportPolling() {
  if (!wikiImportActiveJobIds.value.length) return;
  if (!wikiImportPollTimer) {
    pollWikiImportJobs();
    wikiImportPollTimer = window.setInterval(pollWikiImportJobs, 1600);
  }
}

function stopWikiImportPolling() {
  if (wikiImportPollTimer) {
    window.clearInterval(wikiImportPollTimer);
    wikiImportPollTimer = null;
  }
}

async function loadWikiImportJobs() {
  loading.value.importJobs = true;
  try {
    const data = await api("/api/ingestion/jobs/?limit=100");
    wikiImportJobs.value = data.jobs || [];
    wikiImportJobTotal.value = data.total ?? wikiImportJobs.value.length;
    wikiImportActiveJobIds.value = wikiImportJobs.value.filter(job => job.status === "running").map(job => job.id);
    persistActiveWikiImportJobs();
    if (wikiImportActiveJobIds.value.length) startWikiImportPolling();
    else stopWikiImportPolling();
  } catch (error) {
    if (!handleAdminRequired(error)) status.value = error.message;
  } finally {
    loading.value.importJobs = false;
  }
}

async function openWikiImportJob(job) {
  wikiImportJobId.value = job.id;
  try {
    wikiImportResult.value = await api(`/api/ingestion/jobs/${encodeURIComponent(job.id)}/`);
    if (wikiImportResult.value.status === "running") {
      localStorage.setItem("wikiImportJobId", job.id);
      addActiveWikiImportJob(job.id);
      loading.value.importLayer = true;
      startWikiImportPolling();
    } else {
      loading.value.importLayer = false;
    }
  } catch (error) {
    if (!handleAdminRequired(error)) status.value = error.message;
  }
}

async function retryWikiImportJob(job) {
  loading.value.retryJobId = job.id;
  try {
    const data = await api(`/api/ingestion/jobs/${encodeURIComponent(job.id)}/retry/`, { method: "POST" });
    wikiImportJobId.value = data.id;
    wikiImportResult.value = data;
    localStorage.setItem("wikiImportJobId", data.id);
    addActiveWikiImportJob(data.id);
    updateWikiImportJobInHistory(data);
    wikiImportJobTotal.value += 1;
    loading.value.importLayer = true;
    startWikiImportPolling();
    status.value = `已创建第 ${data.attempt || 2} 次尝试`;
  } catch (error) {
    if (!handleAdminRequired(error)) status.value = error.message;
  } finally {
    loading.value.retryJobId = "";
  }
}

function wikiJobStatusText(job) {
  if (job.status === "running") return `生成中 ${job.progress?.percent || 0}%`;
  if (job.status === "succeeded") return "生成完成";
  if (job.status === "partial_succeeded") return "部分成功，可重试";
  if (job.status === "rate_limited") return "限流中断，可重试";
  if (job.status === "failed") return "生成失败";
  return job.status || "未知状态";
}

function formatJobTime(timestamp) {
  const value = Number(timestamp);
  if (!Number.isFinite(value) || value <= 0) return "-";
  return new Date(value * 1000).toLocaleString("zh-CN", { hour12: false });
}

function updateWikiImportJobInHistory(job) {
  const index = wikiImportJobs.value.findIndex(item => item.id === job.id);
  const summary = { ...job };
  delete summary.logs;
  delete summary.command;
  if (index >= 0) wikiImportJobs.value.splice(index, 1, summary);
  else wikiImportJobs.value.unshift(summary);
}

async function pollWikiImportJobs() {
  if (wikiImportPolling || !wikiImportActiveJobIds.value.length) return;
  wikiImportPolling = true;
  try {
    const activeIds = [...wikiImportActiveJobIds.value];
    const results = await Promise.allSettled(activeIds.map(jobId => api(`/api/ingestion/jobs/${encodeURIComponent(jobId)}/`)));
    let projectsChanged = false;
    for (let index = 0; index < results.length; index += 1) {
      const result = results[index];
      const jobId = activeIds[index];
      if (result.status === "rejected") {
        status.value = result.reason?.message || "生成任务状态查询失败，将自动重试";
        continue;
      }
      const data = result.value;
      updateWikiImportJobInHistory(data);
      if (wikiImportJobId.value === jobId) {
        wikiImportResult.value = data;
        loading.value.importLayer = data.status === "running";
      }
      if (data.status !== "running") {
        removeActiveWikiImportJob(jobId);
        if (["succeeded", "partial_succeeded"].includes(data.status)) {
          projectsChanged = true;
          graphData.value = null;
          status.value = `${data.projectName || "知识项目"}摄入完成`;
        } else if (data.status === "rate_limited") {
          status.value = data.error || "模型接口限流，任务已中断，可在任务记录中重试";
        } else {
          status.value = data.error || "知识摄入失败，可在任务记录中重试";
        }
      }
    }
    if (projectsChanged) {
      versionsChanged();
      await loadProjects();
    }
    if (!wikiImportActiveJobIds.value.length) stopWikiImportPolling();
  } finally {
    wikiImportPolling = false;
  }
}

function clearImportResult() {
  wikiImportResult.value = null;
  wikiImportJobId.value = "";
  localStorage.removeItem("wikiImportJobId");
  loading.value.importLayer = false;
}

async function runSearch(options = {}) {
  const opts = options && !options.target ? options : {};
  loading.value.search = true;
  if (!opts.preserveAnswer) {
    answer.value = "";
    prompt.value = "";
  }
  try {
    const data = await api(projectPath("/search/"), {
      method: "POST",
      body: JSON.stringify({
        query: query.value,
        mode: mode.value,
        versionScope: versionScope.value,
        limit: limit.value,
        modelId: selectedModelId.value,
        useSynonyms: useSynonyms.value,
        useIntent: useIntent.value,
        rerankWithLlm: rerankWithLlm.value,
        rerankThreshold: rerankThreshold.value,
        supplementRawSearch: supplementRawSearch.value
      })
    });
    results.value = uniqueEvidenceResults(data.results || []);
    synonymExpansion.value = data.synonymExpansion || { expandedTerms: [], matchedGroups: [] };
    queryIntent.value = data.queryIntent || { enabled: false, intent: "general", focusTerms: [], searchTerms: [], reason: "" };
    lastSearchQuery.value = data.searchQuery || query.value;
    lastSearchedQuestion.value = query.value;
    searchContextId.value = data.searchContextId || "";
    llmRerank.value = data.llmRerank || { enabled: false, reason: "" };
    searchRuntimeIndex.value = data.searchIndex || null;
    const allResultKeys = results.value.map(itemKey).filter(Boolean);
    selectedEvidenceKeys.value = opts.selectCount ? allResultKeys.slice(0, opts.selectCount) : allResultKeys;
    return data;
  } catch (error) {
    status.value = error.message;
    return null;
  } finally {
    loading.value.search = false;
  }
}

async function searchAndComposeTopThree() {
  const data = await runSearch();
  if (data) {
    await composeAnswer({ useCurrentSelection: true });
  }
}

async function composeAnswer(options = {}) {
  const selectedSnapshot = options.useCurrentSelection !== false ? [...selectedEvidenceKeys.value] : [];
  loading.value.compose = true;
  answer.value = "";
  thinking.value = "";
  prompt.value = "";
  answerMeta.value = "";
  answerEvidenceDocs.value = [];
  composeStage.value = "正在读取文档并生成回答...";
  try {
    await composeAnswerStream(selectedSnapshot);
  } catch (error) {
    status.value = error.message;
  } finally {
    loading.value.compose = false;
    composeStage.value = "";
  }
}

async function composeAnswerStream(selectedSnapshot = [...selectedEvidenceKeys.value]) {
  const selection = selectedEvidencePayload(results.value, selectedSnapshot);
  const canReuseQueryPlan = lastSearchedQuestion.value === query.value;
  const body = JSON.stringify({
    query: query.value,
    mode: mode.value,
    versionScope: versionScope.value,
    limit: limit.value,
    paths: selection.paths,
    chunkIds: selection.chunkIds,
    searchContextId: canReuseQueryPlan ? searchContextId.value : "",
    useSelectedOnly: selection.count > 0,
    maxDocs: selection.count || 8,
    contextMaxChars: contextMaxChars.value,
    contextReserveChars: contextReserveChars.value,
    modelId: selectedModelId.value,
    useSynonyms: useSynonyms.value,
    useIntent: useIntent.value,
    rerankWithLlm: rerankWithLlm.value,
    rerankThreshold: rerankThreshold.value,
    supplementRawSearch: supplementRawSearch.value,
    queryIntent: canReuseQueryPlan ? queryIntent.value : null,
    searchQuery: canReuseQueryPlan ? lastSearchQuery.value : "",
    synonymExpansion: canReuseQueryPlan ? synonymExpansion.value : null
  });
  const response = await fetch(streamProjectPath("/compose-stream/"), {
    method: "POST",
    credentials: "include",
    cache: "no-store",
    headers: { "Content-Type": "application/json" },
    body
  });
  if (!response.ok || !response.body) {
    if (response.status === 401) {
      requireLogin();
      throw new Error("请先登录");
    }
    const text = await response.text();
    throw new Error(text || "流式回答请求失败");
  }

  let finalData = null;
  await readSseStream(response, {
    meta(data) {
      if (data.synonymExpansion) synonymExpansion.value = data.synonymExpansion;
      if (data.queryIntent) queryIntent.value = data.queryIntent;
      if (!results.value.length && data.searchResults) results.value = data.searchResults;
      if (data.documents) {
        answerEvidenceDocs.value = data.documents.map(doc => ({
          ...doc,
          path: doc.path || "",
          chunkId: doc.chunkId ?? null,
          chunkIds: doc.chunkIds || []
        }));
        answerMeta.value = `${data.model || "LLM"} · ${data.documents.length || 0} 份文档`;
      }
    },
    stage(data) {
      if (data.message) {
        composeStage.value = data.message;
        status.value = data.message;
      }
    },
    chunk(data) {
      if (!answer.value) composeStage.value = "正在流式生成回答...";
      answer.value += data.content || "";
    },
    thinking(data) {
      if (!thinking.value && !answer.value) composeStage.value = "正在思考...";
      thinking.value += data.content || "";
    },
    done(data) {
      finalData = data;
      if (data.answer && data.answer !== answer.value) answer.value = data.answer;
      prompt.value = data.llmConfigured ? "" : data.prompt || "";
      if (!answerMeta.value) answerMeta.value = `${data.model || "LLM"}`;
      if (data.llmError) status.value = `LLM 流式失败，已使用本地兜底：${data.llmError}`;
    },
    error(data) {
      throw new Error(data.error || "流式回答失败");
    }
  });
}

async function readSseStream(response, handlers) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder("utf-8");
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const events = buffer.split("\n\n");
    buffer = events.pop() || "";
    for (const rawEvent of events) {
      handleSseEvent(rawEvent, handlers);
    }
  }
  if (buffer.trim()) handleSseEvent(buffer, handlers);
}

function handleSseEvent(rawEvent, handlers) {
  const lines = rawEvent.split(/\r?\n/);
  const eventLine = lines.find(line => line.startsWith("event:"));
  const dataLines = lines.filter(line => line.startsWith("data:"));
  const eventName = eventLine ? eventLine.slice(6).trim() : "message";
  const dataText = dataLines.map(line => line.slice(5).trimStart()).join("\n");
  if (!dataText) return;
  const data = JSON.parse(dataText);
  const handler = handlers[eventName];
  if (handler) handler(data);
}

async function saveSynonyms() {
  loading.value.domain = true;
  try {
    const data = await api(projectPath("/domain-layer/synonyms/"), {
      method: "PUT",
      body: JSON.stringify({ yaml: synonymYaml.value })
    });
    synonymYaml.value = objectToYaml(data.synonyms || {});
    status.value = "synonym.yaml 已保存";
  } catch (error) {
    status.value = error.message;
  } finally {
    loading.value.domain = false;
  }
}

async function importSynonyms() {
  loading.value.domain = true;
  try {
    const data = await api(projectPath("/domain-layer/synonyms/import/"), {
      method: "POST",
      body: JSON.stringify({ text: importText.value, merge: true })
    });
    synonymYaml.value = objectToYaml(data.synonyms || {});
    importText.value = "";
    status.value = "已检测同类词并合并到 synonym";
  } catch (error) {
    status.value = error.message;
  } finally {
    loading.value.domain = false;
  }
}

function objectToYaml(data) {
  return Object.entries(data)
    .map(([key, values]) => `${key}:\n${(values || []).map(value => `  - ${value}`).join("\n")}`)
    .join("\n\n");
}

function clearSelection() {
  selectedEvidenceKeys.value = [];
}

function selectTopResults(count) {
  selectedEvidenceKeys.value = results.value.map(itemKey).filter(Boolean).slice(0, count);
}

function itemPath(item) {
  return item.path || item.filePath || item.filepath || item.relativePath || item.id || "";
}

function itemTitle(item) {
  return item.title || item.name || itemPath(item) || "未命名文档";
}

function itemSnippet(item) {
  return item.evidenceSnippet || item.snippet || item.preview || item.summary || item.content || "";
}

function itemKey(item) {
  return evidenceSelectionKey(item);
}

function formatRelevance(value) {
  const score = Number(value);
  return Number.isFinite(score) ? score.toFixed(2) : value;
}

function retrievalTerms(item) {
  return uniqueTerms((item.retrieval?.matchLocations || []).map(hit => hit.term).concat(item.retrieval?.matchedTerms || [])).slice(0, 10);
}

function expandedMatchedTerms(item) {
  const expanded = new Set((synonymExpansion.value.expandedTerms || []).map(term => String(term).toLocaleLowerCase()));
  return retrievalTerms(item).filter(term => expanded.has(String(term).toLocaleLowerCase()));
}

function citeNumber(part) {
  const match = /\[(\d+)\]/.exec(part?.text || "");
  return match ? Number(match[1]) : 0;
}

function docMatchesItem(doc, item) {
  if (!doc) return false;
  if (item.chunkId && (doc.chunkId === item.chunkId || (doc.chunkIds || []).includes(item.chunkId))) return true;
  if (item.chunkId && (doc.chunkId || (doc.chunkIds || []).length)) return false;
  const path = itemPath(item);
  if (path && doc.path === path) return true;
  if (item.evidenceSourcePath && doc.path === item.evidenceSourcePath) return true;
  return false;
}

function findEvidenceItemForCite(num) {
  const doc = answerEvidenceDocs.value[num - 1];
  if (!doc) return null;
  return results.value.find(item => docMatchesItem(doc, item)) || null;
}

function citeTargetPath(part) {
  const num = citeNumber(part);
  if (!num) return "";
  const item = findEvidenceItemForCite(num);
  return item ? itemPath(item) : "";
}

function relevanceLevelClass(value) {
  const score = Number(value);
  if (!Number.isFinite(score)) return "level-mid";
  if (score >= 0.7) return "level-high";
  if (score >= 0.4) return "level-mid";
  return "level-low";
}

function isResultSelected(item) {
  return selectedEvidenceKeys.value.includes(itemKey(item));
}

function toggleResultSelection(item) {
  const key = itemKey(item);
  if (!key) return;
  const index = selectedEvidenceKeys.value.indexOf(key);
  if (index >= 0) selectedEvidenceKeys.value.splice(index, 1);
  else selectedEvidenceKeys.value.push(key);
}

function setEvidenceEl(path, el) {
  if (!path) return;
  if (el) evidenceEls.set(path, el);
  else evidenceEls.delete(path);
}

function scheduleCiteFlashEnd() {
  if (citeFlashTimer) clearTimeout(citeFlashTimer);
  citeFlashTimer = setTimeout(() => {
    flashingEvidenceKey.value = "";
    citeFlashTimer = null;
  }, 1800);
}

function scrollToEvidence(part) {
  const num = citeNumber(part);
  const item = findEvidenceItemForCite(num);
  if (!item) return;
  const key = itemKey(item);
  const el = evidenceEls.get(key);
  if (!el) return;
  el.scrollIntoView({ behavior: "smooth", block: "center" });
  flashingEvidenceKey.value = key;
  scheduleCiteFlashEnd();
}

const evidenceDocBlocks = computed(() =>
  evidenceDocItem.value ? renderMarkdownBlocks(stripFrontmatter(evidenceDocContent.value)) : []
);

async function openEvidenceDoc(item) {
  evidenceDocItem.value = item;
  evidenceDocContent.value = "";
  evidenceDocError.value = "";
  const path = itemPath(item) || item.evidenceSourcePath;
  if (!path) {
    evidenceDocError.value = "该证据没有可读取的文档路径";
    return;
  }
  loading.value.evidenceDoc = true;
  try {
    // 优先按索引中的结构节点（章节）取证，只读证据所在的这一节；没有节点信息时退化为整篇文档
    const sectionQuery = item.structuralNodeId ? `&structuralNodeId=${encodeURIComponent(item.structuralNodeId)}` : "";
    const editionQuery = item.editionId ? `&editionId=${encodeURIComponent(item.editionId)}` : "";
    const data = await api(projectPath(`/file-content/?path=${encodeURIComponent(path)}${sectionQuery}${editionQuery}`));
    evidenceDocContent.value = data.content || "";
  } catch (error) {
    evidenceDocError.value = error.message;
  } finally {
    loading.value.evidenceDoc = false;
  }
}

function closeEvidenceDoc() {
  evidenceDocItem.value = null;
}

function itemSnippetParts(item) {
  return highlightTextParts(itemSnippet(item), item);
}

function highlightTextParts(text, item) {
  const terms = uniqueTerms([...(item.retrieval?.previewMatchedTerms || []), ...(item.highlightTerms || []), ...(synonymExpansion.value.expandedTerms || []), ...splitSearchTerms(query.value)])
    .filter(term => !isHighlightNoiseTerm(term))
    .filter(term => text.toLowerCase().includes(term.toLowerCase()))
    .sort((a, b) => b.length - a.length);
  if (!text || !terms.length) return [{ text, hit: false }];

  const pattern = new RegExp(`(${terms.map(escapeRegExp).join("|")})`, "gi");
  return text.split(pattern).filter(Boolean).map(part => ({
    text: part,
    hit: terms.some(term => term.toLowerCase() === part.toLowerCase())
  }));
}

function splitSearchTerms(value) {
  return String(value || "")
    .split(/[\s,，。？！；：、,.?!;:()（）\[\]【】]+/)
    .map(part => part.trim())
    .filter(part => part.length >= 2 && !isHighlightNoiseTerm(part));
}

function isHighlightNoiseTerm(value) {
  const raw = String(value || "").trim();
  if (/^(各自|分别)/.test(raw) && /(区别|原则|是什么|哪些|几种|几类)/.test(raw)) {
    return true;
  }
  const clean = raw
    .trim()
    .replace(/^(各自|分别)的?/, "")
    .replace(/^(请问|查询|检索|解释|说明|关于)/, "")
    .replace(/(是什么|是啥|是什么意思|有什么|有哪些|有哪几种|有哪几类|哪几种|哪几类|几种|几类)$/, "")
    .trim();
  return !clean || highlightStopTerms.has(raw) || highlightStopTerms.has(clean);
}

function uniqueTerms(terms) {
  const seen = new Set();
  const unique = [];
  for (const term of terms) {
    const clean = String(term || "").trim();
    const key = clean.toLowerCase();
    if (!clean || seen.has(key)) continue;
    seen.add(key);
    unique.push(clean);
  }
  return unique;
}

function escapeRegExp(value) {
  return String(value).replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

function renderMarkdownBlocks(markdown, options = {}) {
  const blocks = [];
  let currentList = null;
  let currentCode = null;
  const lines = String(markdown || "").replace(/\r\n/g, "\n").split("\n");

  for (let index = 0; index < lines.length; index += 1) {
    const rawLine = lines[index];
    const line = rawLine.trim();
    const fence = line.match(/^```([\w-]*)\s*$/);
    if (fence) {
      currentList = null;
      if (currentCode) {
        blocks.push({ type: "codeblock", language: currentCode.language, text: currentCode.lines.join("\n") });
        currentCode = null;
      } else {
        currentCode = { language: fence[1] || "", lines: [] };
      }
      continue;
    }
    if (currentCode) {
      currentCode.lines.push(rawLine);
      continue;
    }
    if (!line) {
      currentList = null;
      continue;
    }

    if (isMarkdownTableStart(lines, index)) {
      currentList = null;
      const headers = splitMarkdownTableRow(lines[index]);
      index += 2;
      const rows = [];
      while (index < lines.length && isMarkdownTableRow(lines[index])) {
        rows.push(splitMarkdownTableRow(lines[index]));
        index += 1;
      }
      index -= 1;
      blocks.push({ type: "table", headers, rows });
      continue;
    }

    const heading = line.match(/^#{1,6}\s+(.+)$/);
    if (heading) {
      currentList = null;
      blocks.push({ type: "heading", parts: renderInlineMarkdown(heading[1], options) });
      continue;
    }

    const quote = line.match(/^>\s*(.+)$/);
    if (quote) {
      currentList = null;
      blocks.push({ type: "quote", parts: renderInlineMarkdown(quote[1], options) });
      continue;
    }

    const listItem = line.match(/^[-*]\s+(.+)$/) || line.match(/^\d+[.)]\s+(.+)$/);
    if (listItem) {
      if (!currentList) {
        currentList = { type: "list", items: [] };
        blocks.push(currentList);
      }
      currentList.items.push({ parts: renderInlineMarkdown(listItem[1], options) });
      continue;
    }

    currentList = null;
    blocks.push({ type: "paragraph", parts: renderInlineMarkdown(line, options) });
  }

  if (currentCode) {
    blocks.push({ type: "codeblock", language: currentCode.language, text: currentCode.lines.join("\n") });
  }

  return blocks;
}

function renderInlineMarkdown(text, options = {}) {
  const parts = [];
  const pattern = /(\[\[([^\]]+)\]\]|\[([^\]]+)\]\(([^)]+)\)|\*\*[^*]+\*\*|`[^`]+`|\[\d+\])/g;
  let cursor = 0;
  let match;

  while ((match = pattern.exec(text))) {
    if (match.index > cursor) {
      parts.push({ type: "text", text: text.slice(cursor, match.index) });
    }

    const token = match[0];
    if (match[2] !== undefined) {
      const [target, alias] = match[2].split("|");
      const textValue = (alias || target).trim();
      parts.push(options.links ? { type: "link", text: textValue, ref: target.trim() } : { type: "text", text: textValue });
    } else if (match[3] !== undefined) {
      parts.push(options.links ? { type: "link", text: match[3], ref: match[4] || match[3] } : { type: "text", text: match[3] });
    } else if (token.startsWith("**")) {
      parts.push({ type: "strong", text: token.slice(2, -2) });
    } else if (token.startsWith("`")) {
      parts.push({ type: "code", text: token.slice(1, -1) });
    } else {
      parts.push({ type: "cite", text: token });
    }
    cursor = pattern.lastIndex;
  }

  if (cursor < text.length) {
    parts.push({ type: "text", text: text.slice(cursor) });
  }
  return parts.length ? parts : [{ type: "text", text }];
}

function isMarkdownTableStart(lines, index) {
  return isMarkdownTableRow(lines[index]) && index + 1 < lines.length && /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(lines[index + 1]);
}

function isMarkdownTableRow(line) {
  return /^\s*\|.+\|\s*$/.test(String(line || ""));
}

function splitMarkdownTableRow(line) {
  return String(line || "")
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map(cell => cell.trim());
}

function normalizeGraph(rawPayload) {
  const payload = rawPayload?.graph || rawPayload?.data || rawPayload || {};
  const rawNodes = firstArray(payload, ["nodes"]);
  const rawLinks = firstArray(payload, ["links", "edges"]);
  const nodeMap = new Map();
  const links = [];

  for (const rawNode of rawNodes) {
    const id = nodeId(rawNode);
    if (!id) continue;
    nodeMap.set(id, {
      id,
      label: nodeLabel(rawNode, id),
      type: nodeType(rawNode),
      raw: rawNode
    });
  }

  for (const rawLink of rawLinks) {
    const source = endpointId(rawLink, ["source", "from", "sourceId", "source_id", "start", "startNode"]);
    const target = endpointId(rawLink, ["target", "to", "targetId", "target_id", "end", "endNode"]);
    if (!source || !target || source === target) continue;
    ensureGraphNode(nodeMap, source);
    ensureGraphNode(nodeMap, target);
    links.push({
      id: `${source}->${target}-${links.length}`,
      source,
      target,
      label: rawLink.label || rawLink.type || rawLink.relation || "",
      raw: rawLink
    });
  }

  return { nodes: [...nodeMap.values()], links };
}

function selectGraphMode(model, mode) {
  const excluded = mode === "structure"
    ? new Set(["source"])
    : new Set(["mention", "belongs-to"]);
  const links = model.links.filter(link => !excluded.has(String(link.raw?.type || link.label || "").toLowerCase()));
  const connected = new Set(links.flatMap(link => [link.source, link.target]));
  const nodes = model.nodes.filter(node => node.id.startsWith("object:") || connected.has(node.id));
  return { nodes, links };
}

function layoutStructureTree(model, pinnedPositions = {}) {
  const nodes = model.nodes.map(node => ({ ...node }));
  const byId = new Map(nodes.map(node => [node.id, node]));
  const children = new Map();
  const parentOf = new Map();
  const links = model.links
    .map(link => ({ ...link, sourceNode: byId.get(link.source), targetNode: byId.get(link.target) }))
    .filter(link => link.sourceNode && link.targetNode);

  for (const link of links) {
    const type = String(link.raw?.type || link.label || "").toLowerCase();
    if (!["belongs-to", "mention"].includes(type) || parentOf.has(link.source)) continue;
    parentOf.set(link.source, link.target);
    if (!children.has(link.target)) children.set(link.target, []);
    children.get(link.target).push(link.source);
  }

  for (const values of children.values()) {
    values.sort((a, b) => nodeLabel(byId.get(a)?.raw, a).localeCompare(nodeLabel(byId.get(b)?.raw, b), "zh-Hans"));
  }
  const roots = nodes
    .filter(node => !parentOf.has(node.id))
    .sort((a, b) => (a.type === "Source" ? -1 : 1) - (b.type === "Source" ? -1 : 1) || a.label.localeCompare(b.label, "zh-Hans"));
  const visited = new Set();
  let row = 0;

  function place(nodeIdValue, depth) {
    const node = byId.get(nodeIdValue);
    if (!node || visited.has(nodeIdValue)) return node?.y ?? row * 96;
    visited.add(nodeIdValue);
    const childIds = (children.get(nodeIdValue) || []).filter(value => !visited.has(value));
    const childRows = childIds.map(value => place(value, depth + 1));
    node.x = depth * 520;
    node.y = childRows.length ? childRows.reduce((sum, value) => sum + value, 0) / childRows.length : row++ * 96;
    return node.y;
  }

  for (const root of roots) place(root.id, 0);
  for (const node of nodes) place(node.id, 0);
  for (const node of nodes) {
    node.degree = links.filter(link => link.source === node.id || link.target === node.id).length;
    node.radius = node.type === "Source" ? 15 : node.type === "Section" ? 12 : 9;
  }
  centerLayout(nodes);
  applyPinnedGraphPositions(nodes, pinnedPositions);
  declutterLabels(nodes);
  for (const node of nodes) {
    if (["Source", "Section"].includes(node.type)) node.showLabel = true;
  }
  return { nodes, links };
}

function layoutGraph(model, pinnedPositions = {}) {
  const nodes = model.nodes.map(node => ({ ...node }));
  const byId = new Map(nodes.map(node => [node.id, node]));
  const degree = {};
  for (const link of model.links) {
    degree[link.source] = (degree[link.source] || 0) + 1;
    degree[link.target] = (degree[link.target] || 0) + 1;
  }

  for (const node of nodes) {
    node.degree = degree[node.id] || 0;
    node.radius = node.type === "Overview" ? 22 : node.type === "Source" ? 11 : Math.min(18, 10 + node.degree * 1.4);
  }

  const links = model.links
    .map(link => ({ ...link, sourceNode: byId.get(link.source), targetNode: byId.get(link.target) }))
    .filter(link => link.sourceNode && link.targetNode);

  runForceLayout(nodes, links);
  separateOverlaps(nodes);
  spreadGraphComponents(nodes, links);
  centerLayout(nodes);
  applyPinnedGraphPositions(nodes, pinnedPositions);
  declutterLabels(nodes);

  return { nodes, links };
}

function runForceLayout(nodes, links) {
  if (!nodes.length) return;

  // deterministic golden-angle seeding, keeps layouts stable between reloads
  nodes.forEach((node, index) => {
    const angle = index * 2.399963229728653;
    const radius = 70 + Math.sqrt(index) * 42;
    node.x = Math.cos(angle) * radius;
    node.y = Math.sin(angle) * radius;
    node.vx = 0;
    node.vy = 0;
  });

  // 同一对节点之间可能有多条边（source + related），布局时合并成一根弹簧
  const pairMap = new Map();
  for (const link of links) {
    const key = link.source < link.target ? `${link.source}|${link.target}` : `${link.target}|${link.source}`;
    if (!pairMap.has(key)) pairMap.set(key, link);
  }
  const springs = [...pairMap.values()];

  const pairDegree = {};
  for (const link of springs) {
    pairDegree[link.source] = (pairDegree[link.source] || 0) + 1;
    pairDegree[link.target] = (pairDegree[link.target] || 0) + 1;
  }

  const iterations = Math.max(260, Math.min(600, Math.round(200000 / Math.max(nodes.length, 1))));
  const repulsion = Math.max(5200, Math.min(24000, nodes.length * 96));
  const cutoffSquared = 1100 * 1100;
  const forceCap = 80;
  const maxStep = 50;

  for (let step = 0; step < iterations; step++) {
    const alpha = 1 - step / iterations;

    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let distSquared = dx * dx + dy * dy;
        if (distSquared > cutoffSquared) continue;
        if (distSquared < 0.01) {
          dx = (stableHash(a.id + b.id) % 7) * 0.05;
          dy = (stableHash(b.id + a.id) % 7) * 0.05;
          distSquared = dx * dx + dy * dy || 0.01;
        }
        const dist = Math.sqrt(distSquared);
        let force = repulsion / distSquared;
        const gap = a.radius + b.radius + 26 - dist;
        if (gap > 0) force += gap * 0.04;
        if (force > forceCap) force = forceCap;
        const fx = (dx / dist) * force * alpha;
        const fy = (dy / dist) * force * alpha;
        a.vx += fx;
        a.vy += fy;
        b.vx -= fx;
        b.vy -= fy;
      }
    }

    for (const link of springs) {
      const a = link.sourceNode;
      const b = link.targetNode;
      const dx = b.x - a.x;
      const dy = b.y - a.y;
      const dist = Math.max(Math.hypot(dx, dy), 0.01);
      const rest = a.radius + b.radius + 110;
      // d3 风格：高度数节点的弹簧强度衰减，避免枢纽节点把所有邻居拉成一团
      const strength = 1.4 / Math.max(1, Math.min(pairDegree[a.id] || 1, pairDegree[b.id] || 1));
      const force = ((dist - rest) * strength * alpha) / dist;
      const fx = dx * force;
      const fy = dy * force;
      a.vx += fx;
      a.vy += fy;
      b.vx -= fx;
      b.vy -= fy;
    }

    // 单步位移封顶，防止高密度图近距离斥力叠加把布局打飞
    const stepCap = maxStep * Math.max(alpha, 0.12);
    for (const node of nodes) {
      node.vx = (node.vx - node.x * 0.012 * alpha) * 0.82;
      node.vy = (node.vy - node.y * 0.012 * alpha) * 0.82;
      const speed = Math.hypot(node.vx, node.vy);
      if (speed > stepCap) {
        node.vx = (node.vx / speed) * stepCap;
        node.vy = (node.vy / speed) * stepCap;
      }
      node.x += node.vx;
      node.y += node.vy;
    }
  }

  for (const node of nodes) {
    node.x = Math.round(node.x * 10) / 10;
    node.y = Math.round(node.y * 10) / 10;
    delete node.vx;
    delete node.vy;
  }
}

function separateOverlaps(nodes) {
  for (let step = 0; step < 90; step++) {
    let moved = false;
    for (let i = 0; i < nodes.length; i++) {
      const a = nodes[i];
      for (let j = i + 1; j < nodes.length; j++) {
        const b = nodes[j];
        let dx = b.x - a.x;
        let dy = b.y - a.y;
        let dist = Math.hypot(dx, dy);
        if (dist < 0.01) {
          const hash = Math.abs(stableHash(`${a.id}:${b.id}`));
          dx = Math.cos(hash) * 0.1;
          dy = Math.sin(hash) * 0.1;
          dist = 0.1;
        }
        const minDist = a.radius + b.radius + 34;
        if (dist >= minDist) continue;
        const push = (minDist - dist) / 2;
        const ux = dx / dist;
        const uy = dy / dist;
        a.x -= ux * push;
        a.y -= uy * push;
        b.x += ux * push;
        b.y += uy * push;
        moved = true;
      }
    }
    if (!moved) break;
  }
}

function spreadGraphComponents(nodes, links) {
  if (nodes.length < 2) return;
  const nodeMap = new Map(nodes.map(node => [node.id, node]));
  const adjacency = new Map(nodes.map(node => [node.id, []]));
  for (const link of links) {
    adjacency.get(link.source)?.push(link.target);
    adjacency.get(link.target)?.push(link.source);
  }

  const visited = new Set();
  const components = [];
  for (const node of nodes) {
    if (visited.has(node.id)) continue;
    const stack = [node.id];
    const ids = [];
    visited.add(node.id);
    while (stack.length) {
      const id = stack.pop();
      ids.push(id);
      for (const next of adjacency.get(id) || []) {
        if (visited.has(next)) continue;
        visited.add(next);
        stack.push(next);
      }
    }
    const componentNodes = ids.map(id => nodeMap.get(id)).filter(Boolean);
    components.push({ nodes: componentNodes, box: componentBox(componentNodes) });
  }

  if (components.length <= 1) return;
  components.sort((a, b) => b.nodes.length - a.nodes.length);
  const columns = Math.ceil(Math.sqrt(components.length));
  const maxWidth = Math.max(...components.map(component => component.box.width));
  const maxHeight = Math.max(...components.map(component => component.box.height));
  const gap = 180;

  components.forEach((component, index) => {
    const col = index % columns;
    const row = Math.floor(index / columns);
    const targetX = (col - (columns - 1) / 2) * (maxWidth + gap);
    const targetY = row * (maxHeight + gap);
    const dx = targetX - component.box.cx;
    const dy = targetY - component.box.cy;
    for (const node of component.nodes) {
      node.x += dx;
      node.y += dy;
    }
  });
}

function componentBox(nodes) {
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const node of nodes) {
    minX = Math.min(minX, node.x - node.radius);
    maxX = Math.max(maxX, node.x + node.radius);
    minY = Math.min(minY, node.y - node.radius);
    maxY = Math.max(maxY, node.y + node.radius);
  }
  return {
    cx: (minX + maxX) / 2,
    cy: (minY + maxY) / 2,
    width: Math.max(160, maxX - minX),
    height: Math.max(120, maxY - minY)
  };
}

function applyPinnedGraphPositions(nodes, pinnedPositions) {
  for (const node of nodes) {
    const pinned = pinnedPositions[node.id];
    if (!pinned) continue;
    node.x = pinned.x;
    node.y = pinned.y;
  }
}

function declutterLabels(nodes) {
  const placed = nodes.map(node => ({
    x1: node.x - node.radius - 3,
    y1: node.y - node.radius - 3,
    x2: node.x + node.radius + 3,
    y2: node.y + node.radius + 3
  }));
  const ordered = [...nodes].sort((a, b) => (b.degree - a.degree) || (b.radius - a.radius));
  for (const node of ordered) {
    const box = {
      x1: node.x + node.radius + 5,
      y1: node.y - 9,
      x2: node.x + node.radius + 5 + estimateLabelWidth(node.label),
      y2: node.y + 9
    };
    node.showLabel = !placed.some(other => boxesOverlap(box, other));
    if (node.showLabel) placed.push(box);
  }
}

function boxesOverlap(a, b) {
  return a.x1 < b.x2 && a.x2 > b.x1 && a.y1 < b.y2 && a.y2 > b.y1;
}

function estimateLabelWidth(label) {
  let width = 0;
  for (const char of String(label)) {
    width += /[⺀-鿿豈-﫿＀-￯]/.test(char) ? 13.5 : 7.5;
  }
  return width;
}

function centerLayout(nodes) {
  if (!nodes.length) return;
  let minX = Infinity, minY = Infinity, maxX = -Infinity, maxY = -Infinity;
  for (const node of nodes) {
    minX = Math.min(minX, node.x - node.radius);
    maxX = Math.max(maxX, node.x + node.radius);
    minY = Math.min(minY, node.y - node.radius);
    maxY = Math.max(maxY, node.y + node.radius);
  }
  const cx = (minX + maxX) / 2;
  const cy = (minY + maxY) / 2;
  for (const node of nodes) {
    node.x -= cx;
    node.y -= cy;
  }
}

function firstArray(payload, keys) {
  for (const key of keys) {
    if (Array.isArray(payload?.[key])) return payload[key];
  }
  return [];
}

function nodeId(node) {
  if (typeof node === "string") return node;
  return String(node?.id || node?.key || node?.name || node?.label || node?.title || node?.path || "");
}

function nodeLabel(node, fallback) {
  if (typeof node === "string") return node;
  return String(node?.label || node?.name || node?.title || node?.path || fallback);
}

function nodeType(node) {
  if (typeof node === "string") return classifyLabel(node);
  const explicit = node?.nodeType || node?.type || node?.category || node?.kind || node?.group;
  if (explicit) return normalizeType(String(explicit), node);
  return classifyNode(node);
}

function endpointId(link, keys) {
  for (const key of keys) {
    const value = link?.[key];
    if (!value) continue;
    if (typeof value === "object") return nodeId(value);
    return String(value);
  }
  return "";
}

function ensureGraphNode(nodeMap, id) {
  if (!nodeMap.has(id)) {
    nodeMap.set(id, { id, label: id, type: classifyLabel(id), raw: { id } });
  }
}

function normalizeType(type, node = {}) {
  const folded = type.toLowerCase();
  if (folded.includes("source")) return "Source";
  if (folded.includes("section")) return "Section";
  if (folded.includes("overview") || folded.includes("index") || folded.includes("log")) return "Overview";
  if (folded === "object" || folded === "other") return "Object";
  return type.slice(0, 1).toUpperCase() + type.slice(1);
}

function classifyNode(node) {
  const path = String(node?.path || node?.filePath || node?.filepath || node?.relativePath || "").toLowerCase();
  if (path.includes("wiki/sources/")) return "Source";
  if (path.includes("wiki/objects/")) return "Object";
  return classifyLabel(nodeLabel(node, ""));
}

function classifyLabel(label) {
  const value = String(label || "");
  const folded = value.toLowerCase();
  if (folded.startsWith("source:") || folded.includes("/sources/") || folded.endsWith(".md")) return "Source";
  if (folded.includes("wiki index") || folded.includes("wiki log") || folded.includes("project overview")) return "Overview";
  return "Object";
}

function stableHash(value) {
  return String(value).split("").reduce((hash, char) => ((hash << 5) - hash + char.charCodeAt(0)) | 0, 0);
}

function graphColor(type) {
  return {
    Object: "#2563EB",
    Source: "#FF8A34",
    Section: "#10B981",
    Overview: "#98A4B3"
  }[type] || "#38BDF8";
}

function graphNodeSortLabel(node) {
  return stripSourcePrefix(node.label || node.id || "");
}

function graphNodeInitial(label) {
  const first = String(stripSourcePrefix(label) || "").trim().charAt(0);
  if (!first) return "#";
  if (/[A-Za-z]/.test(first)) return first.toUpperCase();
  if (/[0-9]/.test(first)) return "0-9";
  if (/[\u4e00-\u9fff]/.test(first)) return pinyinInitial(first);
  return "#";
}

function pinyinInitial(char) {
  const anchors = [
    ["A", "阿"], ["B", "八"], ["C", "嚓"], ["D", "哒"], ["E", "妸"], ["F", "发"],
    ["G", "旮"], ["H", "哈"], ["J", "讥"], ["K", "咔"], ["L", "垃"], ["M", "妈"],
    ["N", "拿"], ["O", "噢"], ["P", "啪"], ["Q", "七"], ["R", "然"], ["S", "撒"],
    ["T", "他"], ["W", "哇"], ["X", "夕"], ["Y", "丫"], ["Z", "匝"]
  ];
  const collator = new Intl.Collator("zh-Hans-u-co-pinyin");
  let initial = "#";
  for (const [key, anchor] of anchors) {
    if (collator.compare(char, anchor) >= 0) {
      initial = key;
    } else {
      break;
    }
  }
  return initial;
}

function graphGroupSortValue(key) {
  if (key === "0-9") return "00";
  if (key === "#") return "ZZ";
  return key;
}

function toggleGraphNodeGroup(key) {
  collapsedGraphNodeGroups.value = {
    ...collapsedGraphNodeGroups.value,
    [key]: !collapsedGraphNodeGroups.value[key]
  };
}

function selectGraphNodeFromIndex(node) {
  selectGraphNode(node);
}

function resetGraphView() {
  graphView.value = { x: 0, y: 0, scale: 1 };
}

function zoomGraphBy(factor) {
  graphView.value = {
    ...graphView.value,
    scale: Math.max(0.35, Math.min(2.8, graphView.value.scale * factor))
  };
}

function handleGraphWheel(event) {
  zoomGraphBy(event.deltaY > 0 ? 0.9 : 1.1);
}

function startGraphPan(event) {
  if (event.button !== 0) return;
  graphDrag.value = {
    mode: "pan",
    startClientX: event.clientX,
    startClientY: event.clientY,
    startView: { ...graphView.value }
  };
}

function startNodeDrag(event, node) {
  if (event.button !== 0) return;
  const point = graphEventPoint(event);
  if (!point) return;
  graphDrag.value = {
    mode: "node",
    nodeId: node.id,
    offsetX: point.x - node.x,
    offsetY: point.y - node.y
  };
}

function handleGraphPointerMove(event) {
  const drag = graphDrag.value;
  if (!drag) return;
  event.preventDefault();

  if (drag.mode === "pan") {
    const svg = event.currentTarget;
    const scale = graphScreenToViewScale(svg) / graphView.value.scale;
    graphView.value = {
      ...graphView.value,
      x: drag.startView.x + (event.clientX - drag.startClientX) * scale,
      y: drag.startView.y + (event.clientY - drag.startClientY) * scale
    };
    return;
  }

  if (drag.mode === "node") {
    const point = graphEventPoint(event);
    if (!point) return;
    graphNodePositions.value = {
      ...graphNodePositions.value,
      [drag.nodeId]: {
        x: Math.round((point.x - drag.offsetX) * 10) / 10,
        y: Math.round((point.y - drag.offsetY) * 10) / 10
      }
    };
  }
}

function endGraphDrag() {
  graphDrag.value = null;
}

function graphEventPoint(event) {
  const svg = event.currentTarget?.tagName?.toLowerCase() === "svg"
    ? event.currentTarget
    : event.currentTarget?.ownerSVGElement;
  const viewport = svg?.querySelector(".graph-viewport");
  const matrix = viewport?.getScreenCTM();
  if (!svg || !matrix) return null;
  const point = svg.createSVGPoint();
  point.x = event.clientX;
  point.y = event.clientY;
  return point.matrixTransform(matrix.inverse());
}

function graphScreenToViewScale(svg) {
  const [, , width] = graphViewBox.value.split(/\s+/).map(Number);
  return width && svg?.clientWidth ? width / svg.clientWidth : 1;
}

function selectGraphNode(node) {
  selectedGraphNode.value = node;
  loadNodeContent(node);
}

async function loadNodeContent(node) {
  const path = graphNodePath(node);
  nodeContentError.value = "";
  if (node?.raw?.content) return;
  if (!path) {
    nodeContentCache.value = { ...nodeContentCache.value, [node.id]: "" };
    return;
  }
  if (Object.prototype.hasOwnProperty.call(nodeContentCache.value, node.id)) return;

  loading.value.nodeContent = true;
  try {
    const structuralNodeId = node?.raw?.structuralNodeId;
    const sectionQuery = structuralNodeId ? `&structuralNodeId=${encodeURIComponent(structuralNodeId)}` : "";
    const editionQuery = node?.raw?.editionId ? `&editionId=${encodeURIComponent(node.raw.editionId)}` : "";
    const data = await api(projectPath(`/file-content/?path=${encodeURIComponent(path)}${sectionQuery}${editionQuery}`));
    nodeContentCache.value = { ...nodeContentCache.value, [node.id]: data.content || "" };
  } catch (error) {
    nodeContentError.value = error.message;
  } finally {
    loading.value.nodeContent = false;
  }
}

function selectedNodeContent(node) {
  if (!node) return "";
  const rawContent = node?.raw?.content || nodeContentCache.value[node.id] || "";
  return stripFrontmatter(rawContent).trim();
}

function stripFrontmatter(content) {
  return String(content || "").replace(/^---\s*\n[\s\S]*?\n---\s*/, "");
}

function linkifyContentLine(line) {
  const parts = [];
  const pattern = /\[\[([^\]]+)\]\]|\[([^\]]+)\]\(([^)]+)\)/g;
  let cursor = 0;
  let match;
  while ((match = pattern.exec(line))) {
    if (match.index > cursor) {
      parts.push({ text: line.slice(cursor, match.index) });
    }
    if (match[1] !== undefined) {
      // wiki 链接支持 [[目标|别名]]，显示别名、按目标跳转
      const [target, alias] = match[1].split("|");
      parts.push({ text: (alias || target).trim(), ref: target.trim() });
    } else {
      parts.push({ text: match[2], ref: match[3] || match[2] });
    }
    cursor = pattern.lastIndex;
  }
  if (cursor < line.length) {
    parts.push({ text: line.slice(cursor) });
  }
  return parts.length ? parts : [{ text: line }];
}

function selectGraphReference(ref) {
  const node = findGraphNodeByReference(ref);
  if (node) {
    selectGraphNode(node);
  } else {
    status.value = `未在当前图谱中找到引用：${ref}`;
  }
}

function findGraphNodeByReference(ref) {
  const normalizedRef = normalizeReference(ref);
  return graphModel.value.nodes.find(node => {
    const candidates = [
      node.id,
      node.label,
      stripSourcePrefix(node.label),
      graphNodePath(node),
      pathStem(graphNodePath(node))
    ].map(normalizeReference);
    return candidates.includes(normalizedRef);
  });
}

function normalizeReference(value) {
  let text = String(value || "");
  try {
    text = decodeURIComponent(text);
  } catch {
    // Keep the original reference when it is not URI-encoded.
  }
  return text
    .replace(/^\/+/, "")
    .replace(/^\.\/+/, "")
    .replace(/^wiki\//, "")
    .replace(/\.md$/i, "")
    .trim()
    .toLowerCase();
}

function stripSourcePrefix(value) {
  return String(value || "").replace(/^Source:\s*/i, "").replace(/\.(doc|docx|xlsx|xls|pptx|pdf|md|txt|csv|html|htm)$/i, "");
}

function pathStem(path) {
  const clean = String(path || "").replace(/\\/g, "/").replace(/\.md$/i, "");
  return clean.split("/").pop() || clean;
}

function graphNodePath(node) {
  return node?.raw?.path || node?.raw?.filePath || node?.raw?.filepath || node?.raw?.relativePath || "";
}

function linkTouchesNode(link, nodeIdValue) {
  return link.source === nodeIdValue || link.target === nodeIdValue;
}

function isNodeConnected(nodeIdValue, selectedId) {
  if (nodeIdValue === selectedId) return true;
  return graphModel.value.links.some(link => (
    (link.source === nodeIdValue && link.target === selectedId) ||
    (link.target === nodeIdValue && link.source === selectedId)
  ));
}

function nodeDetailRows(node) {
  return Object.entries(node.raw || {})
    .filter(([key, value]) => !["x", "y"].includes(key) && value !== null && value !== undefined && typeof value !== "object")
    .slice(0, 8)
    .map(([key, value]) => ({ key, value: String(value) }));
}

onMounted(async () => {
  await loadCurrentUser();
  if (!currentUser.value.authenticated) return;
  await loadLlmModels();
  await loadProjects();
  loadProjectRefreshInterval();
  if (currentUser.value.isAdmin) {
    await loadWikiImportJobs();
    if (wikiImportJobId.value) {
      const selectedJob = wikiImportJobs.value.find(job => job.id === wikiImportJobId.value);
      if (selectedJob) await openWikiImportJob(selectedJob);
    }
  }
});

onUnmounted(() => {
  stopWikiImportPolling();
  stopProjectRefreshTimer();
});
</script>
