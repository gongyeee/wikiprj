# Local Knowledge Portal 交接说明

更新时间：2026-08-31

项目状态和实施计划统一维护在 [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md)。本文件只记录接手开发和运行维护所需信息，不再维护另一份“后续计划”。

## 当前架构

项目只有本地 Project 数据源：原文目录 + `<project>/.index/search.db`。Project、SourceRoot 和 IngestionJob 保存在 Django 数据库；旧 registry、任务 JSON 和 `structure.json` 仅作为一次性迁移材料。

V6 SQLite 内容库直接保存文档版本、结构节点、检索 chunk、知识对象、词汇、最小 Claim 和证据。搜索、回答和图谱不读取 Markdown；Markdown 只导出到 `exports/wiki`。

## 关键代码

- `backend/tools/ingest_project.py`
  - 读取 DOCX/PDF/Markdown/TXT/CSV。
  - 保留目录，按标题/章节切块，并发调用 LLM。
  - 生成一次性摄入载荷；Markdown 导出由内容库提交后单独重建。

- `backend/api/search_index.py`
  - SQLite 内容/FTS5 索引 V6。
  - DocumentVersion、StructuralNode、RetrievalChunk、KnowledgeObject、ObjectMention、ClaimEvidence。
  - exact-first、对象直连、关联原文局部 FTS 和全项目 FTS 兜底。

- `backend/api/projects.py`
  - 本地项目登记、索引搜索、原文/导出读取和图谱访问。

- `backend/api/views.py`
  - Django API、摄入任务、短期 `searchContextId`、检索与回答编排。
  - `/search` 与 `/compose(-stream)` 复用查询计划和 chunkId。

- `backend/api/llm.py`
  - 意图识别、LLM 配置测试、OpenAI-compatible 回答和 SSE 流式输出。

- `backend/api/models.py`
  - Django Project、ProjectSourceRoot 和 IngestionJob 控制面模型。

- `backend/api/targeted_evidence.py`
  - 原文解析和证据片段处理；核心搜索优先使用索引缓存。

- `frontend/src/App.vue`
  - 检索问答、证据 chunk、知识摄入、索引管理和图谱界面。

## 本地索引结构

索引位置：

```text
<project>/.index/search.db
```

核心表：

- `documents`
- `document_versions`
- `structural_nodes`
- `chunks`
- `chunk_fts`
- `knowledge_objects`
- `object_terms`
- `object_sources`
- `object_mentions`
- `claims`
- `claim_evidence`

以下表只是 exact-first 查询的内部投影，不是知识主数据：

- `entities`
- `entity_aliases`
- `entity_sources`
- `entity_chunk_links`
- `wiki_sections`
- `wiki_section_chunk_links`
- `wiki_object_mentions`
- `index_meta`

索引版本不匹配时会构建 `search.db.tmp`，成功后原子替换旧库。增量更新使用事务与 WAL；解析失败时保留上一版有效索引。

## 查询链路

```text
用户问题
  → 意图识别/同义词扩展
  → 最长对象词项匹配
  → 已关联原文 chunk
  → 关联来源内局部 FTS（有额外限定词）
  → 全项目 FTS（证据不足才执行）
  → searchContextId + chunkId
  → 回答阶段从 SQLite 取证据
```

搜索响应中的 `retrievalMode` 用于判断实际路径：

- `object-linked`
- `object-source-local-fts`
- `global-fts`

## 配置与敏感信息

后端使用 `backend/.env`，不要提交真实密钥。常用字段：

```env
LLM_API_BASE=
LLM_API_KEY=
LLM_MODEL=
LLM_TIMEOUT=90
LLM_STREAM=1
LLM_CONCURRENCY=4
```

## 运行与验证

后端：

```powershell
cd backend
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test
.\.venv\Scripts\python.exe manage.py runserver 127.0.0.1:8091
```

前端：

```powershell
cd frontend
npm install
npm run build
npm run dev
```

索引维护：

```powershell
cd backend
.\.venv\Scripts\python.exe manage.py build_search_index --project-id "项目UUID" --incremental
.\.venv\Scripts\python.exe manage.py verify_search_index --project-id "项目UUID"
```

## 变更交接检查清单

1. 摄入参数、前端表单和 API 文档保持一致。
2. 修改切片、实体或关联算法时升级 `PARSER_VERSION`；数据库结构不兼容时升级 `INDEX_VERSION`。
3. 查询阶段不得重新扫描项目目录或解析原始 Office/PDF 文件。
4. 新检索通道必须返回 `chunkId/headingPath/matchKind`，并在 `searchIndex` 中暴露路径和耗时。
5. 回答链路复用 `/search` 的 `searchContextId` 和已选 chunkId。
6. 数据库迁移先在临时库完成并校验，不能覆盖唯一有效索引。
7. 完成修改后运行后端测试、Django check 和前端生产构建。
8. 同步更新 [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md) 与对应专题/API 文档。

## 已知待办

只保留索引，详细顺序和验收标准见路线图：

- `count` 意图与确定性统计。
- conversations/messages 和上下文问题改写。
- 真实问题集基准。
- 基于评测结果决定是否增加向量召回。
