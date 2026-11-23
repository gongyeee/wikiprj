# LLM 知识库

面向本地规则、接口和业务文档的 Vue + Django 可追溯检索问答系统。Django 数据库管理 Project 和摄入任务；每项目 SQLite 保存文档版本、结构节点、检索切片、知识对象和最小 Claim。

## 当前状态

2026-10-26：项目管理仅负责登记、改名和移除。知识摄入支持从浏览器选择目录／文件上传，并在提交前设置所属文档与版本；服务端已有目录新增文件时，可读取目录并仅勾选新增项。文档版本管理为独立管理员入口，问答框旁只提供查询版本筛选。详见 [文档与版本操作](DOCUMENT_VERSIONS.md)。

查询端中文分词使用 jieba 0.42.1＋项目对象名称/别名词典＋现有意图识别。词典按内容缓存并相互隔离，新增/修改对象后下次检索自动刷新。文件标识符、引号名称和词典完整名称受保护；模型收到本地分词候选用于角色分析，仍不增加模型调用次数。简单对象＋属性用 jieba 词边界回退；没有明确核心对象的普通实词组合在完整短语结果不足时要求多个实词共同命中，不做无差别 OR。分词与角色在问题下方可查看。

内网部署需先安装 `backend/requirements.txt` 中新增的 `jieba==0.42.1`（可提前准备 wheel 离线安装），再更新前端并重启后端。jieba 推理与词典均本地运行，不需外部模型下载；首次加载有初始化开销。本改动只作用查询端，无需重建全文索引。词性/词典并不保证理解所有领域语义，应继续用真实问题验证。

组合短语回退：简单中文“对象＋规则/流程/格式/配置/要求/条件/原理/作用/定义”先查完整短语，结果数量不足时再补充正文命中对象的证据；属性只辅助排序。例如“撮合规则”优先完整命中，再补“撮合”，不独立召回“规则”或“合规”。引号内名称和文件标识符不按此规则拆分。证据说明标识回退原因；无需重建索引。

文档版本管理：在管理控制台的“文档版本”面板初始化已有知识库，给版本填写标签并关联到同一份文档；问答中可选择历史版本，未选择的文档使用各自默认版本。文件内容变化时新增修订，保留旧原文、Chunk 和知识证据，默认版本不会自动切换。版本范围贯穿对象、全文检索、章节展开、回答和引用；范围变化后需重新检索。操作、迁移和接口说明见 [DOCUMENT_VERSIONS.md](DOCUMENT_VERSIONS.md)。

完整短语召回：先提取用户问题中的完整对象、范围和属性；中文规则降级按语法边界保留短语，不将二元/三元字符片段当成词。FTS 用字符片段组合定位候选后，必须在正文、章节或文件字段验证连续完整短语。对象描述关联要求至少四字符完整短语连续命中，作为较低权重的间接证据；不再用两个二字片段重合触发对象关联。证据卡区分命中位置，并提示预览外的正文命中。无需重建现有索引，但无模型时的规则提取较保守，可能少召回改写表达。

核心对象检索：意图识别同时返回 `coreObjects`、`scopeTerms`、`requestedAttribute`，无需额外模型调用；只接受用户原话中的候选，关闭模型时仍可识别带下划线的标识符和引号词。每个对象及词典已知别名独立召回并验证正文连续命中，再用章节路径和文档范围排序。高置信度对象已有命中时不补入纯背景；未命中时保留背景并明确标注。比较问题优先覆盖多个对象。证据卡展示核心对象实际命中词、范围和询问属性；不需要重建索引。

按文档重新摄入：在管理控制台的生成参数中填写“仅重新导入这些文档”，每行填写一个相对原文目录的路径。系统只解析和抽取所选原文：同内容重抽取更新该版知识，内容变化时新增版本，保留历史 Chunk、FTS、Mention 和关系证据。留空仍为全目录摄入。需要已有完整 V7 索引；处理中使用独立 SQLite 副本，成功校验后发布，失败时线上旧索引不变。如果期间原文或项目索引发生变化，会拒绝发布并提示重试。此入口不同于仅更新解析缓存的“增量更新索引”。

知识对象身份共享，描述、别名和关键词按文档版本来源保存；检索仅使用所选版本贡献的信息。旧抽取数据缺少来源时需重新摄入，过渡期间不会用无法确定来源的共享描述作答。临时索引保存在 `.index/ingestion-<任务ID>.db`，用于失败排查。

截至 2026-09-02：

- V7 内容库直接保存 DocumentVersion、真实章节树、RetrievalChunk、KnowledgeObject、ObjectTerm、ObjectMention、Claim 和 ClaimEvidence。
- DOCX 按 Word 大纲/标题样式建树，保留段落、列表和表格文本语义；检索 chunk 不跨章节。
- 摄入任务通过一次性载荷原子写库；运行时不索引或解析 Markdown，也不生成 `structure.json`。
- Markdown 默认只读导出到 `exports/wiki`，删除导出目录不影响搜索、回答或图谱。
- 检索采用完整对象词汇优先，并能直接定位结构节点和原文 chunk；证据充分时跳过全项目 FTS。
- `/search` 与 `/compose-stream` 通过 `searchContextId + chunkId` 复用意图；Compose 将 chunk 展开为最深命中章节全文，并补充祖先章节公共正文。
- 管理员问答评测支持 Excel 导入、批量维护/迁移、后台评分、完整答案对照、人工复核与报告导出；真实问题集与评分校准待业务验收，见 [EVALUATION.md](EVALUATION.md)。
- 多轮 conversation/message、确定性统计意图仍待实施。
- 是否增加向量数据库将在真实问题集评测后决定。

完整进度、后续顺序和验收标准见 [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md)。去除 `nashsu/llm_wiki` 兼容后的目标架构与迁移决策见 [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md)。

## 核心能力

- 使用 UUID 管理本地 Project 和来源目录。
- 从 DOCX、PDF、Markdown、TXT、CSV 直接摄入版本化内容模型。
- 按真实章节边界和长段落切片，不跨章节，不限制每文档最多处理块数。
- SQLite FTS5 持久化全文/切片索引，查询阶段不重复解析 DOCX/PDF。
- 对象规范名、别名、连续短语和中文二元/三元检索。
- ObjectMention/ClaimEvidence 到文档版本、结构节点和原文 chunk 的持久化关联。
- 对象直连 → 关联原文局部 FTS → 全项目 FTS 的分层短路。
- 项目级 `synonym.yaml` 同义词扩展。
- 检索结果选择、流式回答、原文证据和引用。
- 管理员知识摄入、索引增量更新/全量重建和状态检查。
- 管理员原文解析与 Chunk 检查器，可核对章节树、切片、对象/Claim 和最终问答证据。

## 架构概览

```text
原始文件
  → DocumentVersion / StructuralNode / RetrievalChunk
  → KnowledgeObject / ObjectMention / ClaimEvidence
  → <project>/.index/search.db + FTS5
  → exact object / evidence link / scoped FTS / global FTS fallback
  → searchContextId + chunkId → 完整最深章节 + 祖先公共正文
  → OpenAI-compatible LLM 回答
```

## 快速启动

### 后端

```powershell
cd backend
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
python manage.py migrate
python manage.py runserver 127.0.0.1:8091
```

编辑 `backend/.env`：

```env
LLM_API_BASE=https://api.openai.com/v1
LLM_API_KEY=your_llm_api_key
LLM_MODEL=gpt-4.1-mini
LLM_STREAM=1
LLM_CONCURRENCY=4
```

### 前端

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1
```

生产构建检查：

```powershell
cd frontend
npm run build
```

内网部署端口和权限说明见 [LAN_START.md](LAN_START.md)。

## 常用维护命令

```powershell
cd backend

# Django 配置检查和测试
.\.venv\Scripts\python.exe manage.py check
.\.venv\Scripts\python.exe manage.py test

# 增量构建本地项目索引
.\.venv\Scripts\python.exe manage.py build_search_index --project-id "项目UUID" --incremental

# 全量重建/版本迁移
.\.venv\Scripts\python.exe manage.py build_search_index --project-id "项目UUID" --force

# 完整性检查
.\.venv\Scripts\python.exe manage.py verify_search_index --project-id "项目UUID"
```

知识摄入与只读 Markdown 导出说明见 [INGESTION_AND_EXPORT.md](INGESTION_AND_EXPORT.md)。

## 主要 API

- `GET /api/health/`
- `GET /api/projects/`
- `POST /api/projects/{project_id}/search/`
- `POST /api/projects/{project_id}/compose/`
- `POST /api/projects/{project_id}/compose-stream/`
- `GET|POST /api/projects/{project_id}/search-index/`
- `GET /api/projects/{project_id}/index-inspector/`（仅管理员）
- `POST /api/ingestion/build/`
- `GET /api/ingestion/jobs/{job_id}/`
- `GET /api/projects/{project_id}/graph/`
- `GET|PUT /api/projects/{project_id}/domain-layer/...`

完整请求与响应格式见 [API_README.md](API_README.md)。

## 文档导航

日志留存和操作审计说明见 [AUDIT_RETENTION.md](AUDIT_RETENTION.md)，包含服务内自动维护、默认留存期限和管理后台入口。

| 文档 | 用途 |
| --- | --- |
| [TECHNICAL_ARCHITECTURE_AND_HIGHLIGHTS.md](TECHNICAL_ARCHITECTURE_AND_HIGHLIGHTS.md) | 当前技术架构、项目亮点、文档版本管理设计与能力边界 |
| [ARCHITECTURE_V2.md](ARCHITECTURE_V2.md) | 去除上游兼容后的目标架构、数据模型、迁移阶段和待确认决策 |
| [PROJECT_ROADMAP.md](PROJECT_ROADMAP.md) | 当前进度、下一阶段和验收标准 |
| [PROJECT_HANDOFF.md](PROJECT_HANDOFF.md) | 运行维护、关键代码和交接清单 |
| [INGESTION_AND_EXPORT.md](INGESTION_AND_EXPORT.md) | 知识摄入、Markdown 导出和索引维护 |
| [SEARCH_OBJECT_CHUNK_LINKS.md](SEARCH_OBJECT_CHUNK_LINKS.md) | 知识对象到原文 chunk 的关联及检索短路标准 |
| [API_README.md](API_README.md) | API 契约和调用示例 |
| [LAN_START.md](LAN_START.md) | 内网启动与账户权限 |

## 当前限制

- V7 不导入 V6 或旧 `structure.json` 知识数据；升级直接从原文重建，KnowledgeObject/Claim 需重新摄入。
- `searchContextId` 是短期检索上下文，不等同于多轮会话记忆。
- “统计 X 有多少个”尚无独立确定性统计链路。
- 文件接口可以浏览原文和 Markdown 导出；搜索、回答和图谱只读取 V7 内容库。
- 向量召回尚未实施，也不是当前默认依赖。
