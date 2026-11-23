# 架构 V2：去除 nashsu/llm_wiki 兼容后的本地知识系统

状态：V7 Word 章节树与完整章节取证已实施；V6 不兼容升级，V7 知识重新摄入与质量验收待执行
创建日期：2026-08-31  
适用范围：后端数据模型、文档摄入、检索、回答、项目管理和 Wiki 展示

## 1. 背景

项目最初同时支持两类数据源：

1. 通过 HTTP 代理 `nashsu/llm_wiki` 项目。
2. 登记本地目录，生成兼容其目录约定的 Markdown Wiki，再建立本地 SQLite 搜索索引。

当前产品目标已经转向本地规则、接口和业务文档的可追溯问答。核心能力是结构化解析、原文证据定位、确定性检索、统计、多轮问答和评测，而不是维护一个与上游格式兼容的 Markdown Wiki。因此现有链路存在不必要的中间转换：

```text
原文
  → 结构化抽取
  → wiki/*.md + structure.json
  → 重新解析 Markdown/frontmatter
  → SQLite 对象和关联
  → 检索与回答
```

V2 将其改为：

```text
原文
  → 文档版本与结构节点
  → 检索 chunk
  → 知识对象、事实声明和证据定位
  → SQLite FTS5 检索
  → 回答、图谱和可选 Markdown 视图
```

## 2. 目标

1. 只有一套本地Project、Search、Compose 和 Graph 链路。
2. 结构化数据库是知识主数据；Markdown 和搜索索引均为可重建投影。
3. 原文版本、章节结构、检索 chunk 和引用证据的身份边界清晰。
4. 用统一知识对象取代含义不稳定的 concept/entity 两套顶层分类。
5. alias、同义词、检索词和标签各自有明确语义。
6. 规则、条件、接口约束等事实可以携带来源、版本和精确证据。
7. 保留已验证有效的 exact-first、局部 FTS、原子索引构建和证据短路。
8. 每个迁移阶段都可验证、可停止，不要求一次性重写全部系统。

## 3. 非目标

以下内容不因本次重构自动进入范围：

- 不更换 Django、Vue 或 SQLite。
- 不预先引入向量数据库、图数据库、消息队列或新的 ORM。
- 不在缺少真实问题集时重写检索评分算法。
- 不建设完整 OWL/RDF 本体系统。
- 不把 Markdown 重新定义为可与数据库双向编辑的第二主数据源。
- 不在知识主模型稳定前实施 Conversation/Message 或确定性统计。

## 4. 架构原则

### 4.1 单一主数据

- 原始文件是事实来源，不由系统修改。
- 解析后的文档结构、知识对象、声明和证据定位以数据库记录为准。
- FTS、图谱响应、摘要页和 Markdown 均可由主数据重建。
- 不再通过解析生成后的 Markdown 反向恢复知识对象。

### 4.2 控制面与内容面分离

控制面放入 Django 数据库：

- Project；
- ProjectSourceRoot；
- IngestionJob；
- LLM 配置引用；
- 后续的 Conversation/Message；
- 用户、权限和审计信息。

内容面默认继续使用每项目 SQLite：

- 文档及版本；
- 结构节点和检索 chunk；
- 知识对象和词汇；
- mention、claim 和证据；
- FTS 表及索引元数据。

理由：控制面需要事务、权限和跨项目查询；内容索引需要便携、可重建，并且当前 SQLite 性能已满足规模要求。

### 4.3 身份与版本分离

- `document_id` 表示逻辑文档。
- `document_version_id` 表示一次确定内容的版本。
- `structural_node_id` 只在一个文档版本内标识节点。
- `logical_node_key` 可选，用于把不同版本中的对应章节关联起来。
- `chunk_id` 是检索投影身份，不作为长期业务引用。
- 长期引用使用文档版本、结构节点和 evidence span。

## 5. 目标领域模型

本节是逻辑模型；字段名和表名在实施时可以按现有代码惯例微调。

### 5.1 Project

```text
Project
- id: UUID
- name
- status
- created_at / updated_at

ProjectSourceRoot
- project_id
- absolute_path
- include_patterns
- exclude_patterns
```

不再使用 `manual:` 前缀，也不通过绝对路径哈希生成业务 ID。`raw/sources` 可以作为导入时默认值，但不是领域协议。

### 5.2 文档结构

```text
SourceDocument
- id
- project_id
- source_root_id
- relative_path
- title
- media_type
- status

DocumentVersion
- id
- document_id
- content_hash
- parser_version
- extracted_text
- created_at

StructuralNode
- id
- document_version_id
- parent_id
- node_type: document | section | paragraph | list | table | page
- ordinal
- heading
- heading_path
- page_no
- start_offset / end_offset
- content_hash

RetrievalChunk
- id
- document_version_id
- chunk_no
- heading_path
- start_offset / end_offset
- content
- content_hash
```

`StructuralNode` 保存文档本身的层级；`RetrievalChunk` 只服务搜索，可以随切片算法升级重建。二者不得再次混为“section/chunk”。

### 5.3 统一知识对象

```text
KnowledgeObject
- id
- project_id
- canonical_name
- normalized_name
- description
- ontology_kind: individual | class | concept | unspecified
- domain_type
- status: candidate | verified | deprecated
- created_at / updated_at
```

第一版 `domain_type` 使用小型受控集合：

```text
organization | role | system | product | document
interface | message | event | process
rule | condition | metric | term | other
```

原则：

- concept/entity 不再决定表、目录和检索逻辑。
- 不再提供旧 concept/entity 或 `structure.json` 导入器；V7 只从原文和当前摄入载荷构建。
- 无法可靠分类时使用 `other` 或 `unspecified`，不让 LLM 被迫伪精确。
- 类型集合先保持小；只有真实数据持续无法表达时才增加。

### 5.4 词汇层

```text
ObjectTerm
- id
- object_id
- text
- normalized_text
- term_kind: canonical | alias | abbreviation | spelling_variant | search_term
- provenance: extracted | configured | human
- status

Tag
- id
- project_id
- name

ObjectTag
- object_id
- tag_id
```

规则：

- keyword 不再是一级知识对象。
- 同义词配置迁入 `ObjectTerm`，不再与对象 aliases 并行维护。
- related term 不得标为 alias；否则 exact-first 会扩大错误命中。
- 普通全文关键词由 FTS/tokenizer 处理，不持久化。

### 5.5 Mention、Claim 与证据

```text
ObjectMention
- id
- object_id
- document_version_id
- structural_node_id
- start_offset / end_offset
- matched_text
- extraction_summary
- confidence

Claim
- id
- project_id
- subject_object_id
- predicate
- object_object_id          # 与对象的关系
- literal_value             # 或字面值
- qualifiers_json           # 条件、范围、时间、版本、例外
- status: candidate | verified | contradicted | deprecated

ClaimEvidence
- claim_id
- document_version_id
- structural_node_id
- start_offset / end_offset
- quoted_text
- confidence
```

第一阶段只要求把现有显式关系迁为带证据的 Claim，不建设通用推理引擎。

## 6. 写入与读取路径

### 6.1 摄入

```text
登记 Project
→ 扫描 SourceRoot
→ 创建/识别 SourceDocument
→ 内容变化时创建 DocumentVersion
→ 解析 StructuralNode
→ 构建 RetrievalChunk 与 FTS
→ LLM 按结构节点抽取候选对象、mention、claim
→ 项目级规范化与去重
→ 事务提交
→ 可选生成 Markdown 展示
```

要求：

- 未变化文档不得重复调用 LLM。
- 新版本成功提交前保留上一有效版本。
- LLM 失败不得覆盖上一有效知识结果。
- 对象抽取上限只控制一次请求的候选量，不得作为项目级知识保留上限。

### 6.2 检索

```text
Query
→ 意图与词汇规范化
→ ObjectTerm 最长非重叠匹配
→ ObjectMention / ClaimEvidence 直连
→ 关联文档内局部 FTS
→ 证据不足时全项目 FTS
→ EvidenceHit[]
```

统一结果：

```text
EvidenceHit
- chunk_id
- document_id / document_version_id
- structural_node_id
- title / heading_path
- snippet
- score
- match_kind
- matched_terms
- evidence_level: source | extracted_summary
```

同一文档允许返回多个不同章节的 chunk；通过文档多样性限制控制重复，而不是按路径只留一个结果。

### 6.3 回答

- LLM 输入以 `source` 证据为主。
- 抽取摘要只用于定义解释、查询扩展和排序，不与原文处于同一证据等级。
- 引用保存长期证据定位，不保存易变的整数 chunk ID 作为唯一引用。
- `searchContextId` 继续用于短期搜索/回答复用。

## 7. Markdown 与图谱

### 7.1 Markdown

建议默认保留只读 Markdown 导出能力：

```text
exports/wiki/
  index.md
  objects/
  sources/
```

- Markdown 由数据库生成，不参与摄入和索引。
- 删除 `concepts/entities/keywords` 的领域含义；如为旧工具导出，可由导出器映射。
- 不支持数据库与 Markdown 双向合并。
- 用户手工编辑导出文件不会回写主数据。

### 7.2 图谱

图谱直接读取：

- KnowledgeObject；
- Claim；
- ObjectMention → SourceDocument。

不再扫描 wikilink，也不把方向性关系折叠成无向边。前端可以选择无向展示，但 API 保留原始方向和证据。

## 8. 上游兼容移除边界

删除运行时能力：

- `LlmWikiClient` 及 `LLM_WIKI_*` 配置；
- 上游 health/projects/files/search/chat/graph 代理；
- `manual` 与 upstream 的条件分支；
- `llmWikiAvailable`、上游 `raw` 搜索响应；
- 由 `wiki/concepts`、`wiki/entities`、frontmatter 决定对象类型的逻辑。

可选保留一次性导入器：

- 输入旧项目根目录；
- 优先导入原始文档；
- 可读取旧 frontmatter/structure.json 作为候选元数据；
- 写入 V2 主数据后不再依赖旧目录；
- 导入器不进入正常搜索和回答链路。

## 9. 分阶段实施

### 阶段 0：决策与基线

状态：已完成。

1. 确认本文第 11 节决策。
2. 固定现有测试与一组代表性查询。
3. 备份项目登记数据和项目索引。
4. 不改变生产数据格式。

验收：关键决策全部标记为“已决定”，现有测试通过。

### 阶段 1：统一 Project 控制面

1. 用 Django 模型保存本地 Project 和 SourceRoot。
2. 提供现有项目 registry 的一次性迁移。
3. API 统一使用 UUID project ID。
4. 搜索实现暂时继续复用 V4 索引。

验收：项目列表、注册、移除、索引维护不依赖上游服务或 JSON registry；现有本地项目可访问。

### 阶段 2：内容模型 V7（已完成）

1. 已新建文档版本、结构节点、知识对象、词汇、mention、claim 表。
2. 摄入流程通过一次性载荷直接写 V7 数据库。
3. DOCX 根据 Word 大纲/标题样式建立真实父子章节，段落、列表、表格挂到所属最深章节。
4. RetrievalChunk 只在章节内部生成并指向该章节；FTS 直接从 RetrievalChunk 构建。
5. V6 与旧 `structure.json` 不导入 V7。
6. LLM 摄入只消费已有 RetrievalChunk，抽取载荷直接携带 Chunk、StructuralNode 和 DocumentVersion 标识。
7. ObjectMention 与 ClaimEvidence 直接引用标准 Chunk；已删除 `entities/wiki_sections` 兼容投影和抽取后文字回查定位。
8. 可见结构路径统一为“相对目录 > 文件名 > 章节层级”。

验收：无需 Markdown 或 structure.json 即可完成构建、搜索、图谱和原文定位。

### 阶段 3：统一运行时链路（已完成）

1. 删除 upstream/manual 搜索和回答分支。
2. Search 返回统一 `EvidenceHit`。
3. Compose 用 chunk ID 定位最深章节，读取完整章节，并补充祖先章节的直接公共正文；不读取同级其他章节。
4. 图谱改查 KnowledgeObject/Claim/Mention。

最小 Claim 从摄入任务的结构化载荷直接写入。搜索、回答和图谱不读取 Markdown；Compose 按 V7 章节树取完整原文证据。

验收：关闭或删除上游配置不影响任何功能；API 不再返回兼容字段。

### 阶段 4：Markdown 降级为投影（已完成）

1. Markdown 默认导出到 `exports/wiki`。
2. 已停止索引 Markdown。
3. 已停止生成 `structure.json`。
4. 已删除运行时 Wiki 图谱、回答和检索解析代码；仅保留一次性旧项目导入器。

验收：删除导出目录后仍可完整搜索、回答、统计和重建导出。

### 阶段 5：能力建设

顺序：

1. 真实问题集评测；
2. 确定性统计；
3. Conversation/Message；
4. 根据评测决定是否增加向量召回。

## 10. 迁移、回滚与数据安全

- 所有 V7 首次构建写入临时数据库，验证后原子切换。
- 不就地修改唯一有效的 V4 `search.db`。
- 旧原文、Wiki 和 registry 在迁移验收前只读保留。
- 每项目记录迁移状态、来源版本、开始时间、结束时间和失败原因。
- 回滚保留旧索引备份或重新构建，不反向转换 V7 数据。
- 删除旧兼容代码和旧文件必须是最后一步，并由单独变更完成。

## 11. 待确认决策

### D1：Markdown 的产品定位

决定：保留“只读、可删除、可重建”的 Markdown 导出，不支持双向编辑。

这里的“不支持反向写入”是指：数据库生成 Markdown 后，如果用户或外部工具修改了导出的 Markdown，系统不会再解析这些修改并更新 `KnowledgeObject`、`Claim` 等数据库记录。Markdown 是输出，不是另一套编辑入口。

示例：数据库中对象“交易系统”的描述为 A，导出后用户把 Markdown 描述改成 B。下一次重新导出时仍以数据库中的 A 为准，Markdown 中的 B 不会自动进入数据库，并可能被覆盖。

状态：已确认（2026-08-31）。未来需要提供独立的知识核对和人工修改入口，由该入口直接修改数据库并保留审计信息；本版不实施该界面和工作流。Markdown 人工修改不进入知识库。

### D2：旧项目迁移范围

决定：保留一次性导入器，支持当前 `manual_wiki_projects.json` 中已经登记的本地项目；不迁移上游 nashsu 服务中的项目，也不再支持连接正在运行的 nashsu 服务。

状态：已确认（2026-08-31）。

### D3：Project 与内容数据库位置

决定：Project/Job 使用 Django 数据库；每项目知识与索引继续放在项目目录的 SQLite 中。

状态：已确认（2026-08-31）。当前不按多实例共享设计；未来若出现该需求，再重新评估每项目 SQLite。

### D4：知识类型集合

决定：采用第 5.3 节的小型集合，并允许 `other/unspecified`；以后基于真实数据扩充。

状态：已确认（2026-08-31）。当前没有必须遵守的领域分类标准或固定枚举。

### D5：Claim 实施深度

决定：第一版只保存现有显式三元关系、qualifier JSON 和证据，不做自动推理或冲突合并。

三元关系表示“主体—关系—客体”，例如：

```text
竞价订单确认消息 — 由…处理 — 交易系统
登录流程 — 依赖 — 鉴权服务
```

条件 JSON 保存不能自然放进主体、关系、客体三个位置的限定信息，例如：

```json
{
  "when": "订单校验通过后",
  "scope": "集合竞价阶段",
  "effectiveFrom": "2026-01-01",
  "exception": "系统维护期间除外"
}
```

它只是结构化保存条件、适用范围、生效时间和例外；第一版不依据这些字段自动推导新结论。

状态：已确认（2026-08-31）。采用最小 Claim；本版没有规则推理、冲突自动处理或复杂规则引擎。

## 12. 完成定义

本次架构重构完成需同时满足：

1. 运行时不依赖 `nashsu/llm_wiki` 服务、API 或目录协议。
2. 项目没有 manual/upstream 双链路。
3. 删除 Markdown/导出目录不影响知识库核心功能。
4. concept/entity/keyword 不再是相互竞争的顶层存储类型。
5. 任一知识对象或 claim 能追溯到文档版本和原文证据。
6. 检索质量不低于重构前固定基线。
7. 旧项目迁移和回滚流程经过至少一个真实项目验证。
