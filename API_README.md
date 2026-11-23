# LLM 知识库 API 说明

所有接口前缀 `/api/`。鉴权使用 Django session cookie：登录后所有请求需 `credentials: "include"`（浏览器）或 `-b cookies.txt`（curl）。

业务接口均要求登录，未登录或会话失效返回 `401 {"error": "请先登录", "needLogin": true}`。健康检查、登录和身份查询可匿名调用；MCP 接口使用独立 Bearer 令牌认证。

管理员问答评测接口位于 `/api/evaluation/`，覆盖标准问答、Excel 导入、迁移、任务、人工复核和报告。全部接口要求管理员；写请求还需 `config/` 返回的 CSRF token。详见 [EVALUATION.md](EVALUATION.md)。

## 账户与权限

摄入请求可传 `sourceFiles: ["交易规则/接口说明.docx"]`，路径相对 `rawDir`，支持多个文件；不传或传空数组仍为全目录摄入。指定文档时忽略 `limitDocs`，仅更新所选文档的索引与抽取，失败重试保持原来的选择。脚本对应参数为可重复的 `--source-file`；脚本输出载荷与暂存数据库，由服务端校验后统一发布。

### 角色说明

| 角色 | 能力 |
| --- | --- |
| 匿名 | 健康检查、登录和查询当前登录状态 |
| 普通用户 | 登录后使用检索问答、关系图谱、查看文档内容、查看领域知识层 |
| 管理员（is_staff） | 全部能力 + 知识摄入、登记/移除本地项目、查询摄入任务 |

创建管理员账户：

```powershell
cd backend
.\.venv\Scripts\python.exe manage.py migrate
.\.venv\Scripts\python.exe manage.py createsuperuser
```

### POST /api/auth/login/

登录并写入 session cookie。

请求体：

```json
{ "username": "admin", "password": "your-pass" }
```

成功 200：

```json
{
  "authenticated": true,
  "username": "admin",
  "isAdmin": true,
  "isSuperuser": true,
  "userId": 1
}
```

失败 401：

```json
{ "error": "用户名或密码错误" }
```

### GET /api/auth/me/

返回当前会话身份。匿名也可调用。

未登录 200：

```json
{ "authenticated": false, "isAdmin": false, "username": "" }
```

已登录 200：

```json
{
  "authenticated": true,
  "username": "admin",
  "isAdmin": true,
  "isSuperuser": true,
  "userId": 1
}
```

### POST /api/auth/logout/

登出当前 session。无请求体。

```json
{ "ok": true }
```

## 基础接口（除健康检查外需登录）

### GET /api/health/

健康检查，无需登录。

```json
{
  "portal": "ok"
}
```

### GET /api/projects/

列出 Django 数据库中登记的本地项目。

```json
{
  "projects": [
    { "id": "2886e610-...", "name": "法规库", "path": "D:/...", "rawDir": "D:/.../sources", "exportDir": "D:/.../exports/wiki" }
  ]
}
```

### GET /api/projects/{project_id}/files/

列出项目内文件树。

```json
{ "items": [ { "path": "raw/sources/rules.docx", "name": "rules.docx" }, { "path": "exports/wiki/index.md", "name": "index.md" } ] }
```

### GET /api/projects/{project_id}/graph/

从 KnowledgeObject、Claim 和 ObjectSource 返回有向知识图谱。

```json
{
  "nodes": [ { "id": "object:obj_xxx", "label": "认购邀请书", "type": "document", "path": "" } ],
  "links": [ { "source": "object:obj_xxx", "target": "object:obj_yyy", "type": "适用于", "claimId": "clm_xxx", "directed": true, "evidenceCount": 1 } ]
}
```

### GET /api/projects/{project_id}/file-content/?path=raw/sources/rules.docx

读取单个文件正文。

```json
{ "path": "raw/sources/rules.docx", "content": "..." }
```

## 检索与问答

### POST /api/projects/{project_id}/search/

仅检索，不调用 LLM。

请求体：

```json
{
  "query": "认购邀请书的作用",
  "limit": 8,
  "useSynonyms": true
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `query` | 是 | 用户问题 |
| `limit` / `topK` | 否 | 结果数量，默认 8 |
| `useSynonyms` | 否 | 是否启用 synonym.yaml 扩展，默认 true |

返回：

```json
{
  "results": [
    {
      "path": "raw/sources/接口说明.docx",
      "chunkId": 183,
      "documentId": 12,
      "documentVersionId": 19,
      "structuralNodeId": "nod_xxx",
      "documentPath": "交易规则/接口说明.docx",
      "headingPath": "竞价交易 > 订单确认消息",
      "pageNo": 12,
      "title": "...",
      "score": 12,
      "snippet": "...",
      "evidenceSnippet": "...",
      "evidenceKind": "source",
      "evidenceSourcePath": "D:/.../xxx.docx",
      "highlightTerms": ["认购邀请书", ...],
      "llmRelevance": 0.82,
      "llmRelevanceReason": "...",
      "matchKind": "exact-object",
      "matchedTerms": ["竞价订单确认消息"]
    }
  ],
  "synonymExpansion": {
    "query": "认购邀请书的作用",
    "expandedQuery": "认购邀请书的作用 认购邀请书",
    "expandedTerms": ["认购邀请书"],
    "matchedGroups": [ { "canonical": "认购邀请书", "terms": [...] } ]
  },
  "searchContextId": "abc123...",
  "searchIndex": {
    "ok": true,
    "indexVersion": 6,
    "retrievalMode": "object-linked",
    "globalFtsUsed": false,
    "objectLinkedChunks": 4,
    "linkedSourceDocuments": 1,
    "fallbackReason": "",
    "elapsedMs": 5.2
  },
  "queryPlan": {
    "strategy": "exact-first",
    "exactObjects": ["竞价订单确认消息"],
    "exactPhrases": [],
    "fallbackTerms": ["竞价", "订单", "确认消息"]
  },
  "highlightTerms": ["认购邀请书", ...],
  "llmRerank": { "enabled": false, "reason": "未启用" }
}
```

`matchKind` 可能为：`object-source-structured`（ObjectMention 结构节点直连）、`object-source-exact`（对象名称/别名直连）、`object-source-evidence`、`exact-object`、`exact-title`、`exact-phrase` 或 `fulltext`。

### POST /api/projects/{project_id}/compose/

**核心问答接口**：检索 → 用 chunkId 定位 V7 最深章节 → 读取完整章节及祖先公共正文 → 调用 LLM 生成带引用的回答。

请求体：

```json
{
  "query": "上市公司再融资中认购邀请书的作用是什么？",
  "limit": 6,
  "maxDocs": 8,
  "paths": [],
  "chunkIds": [183, 194, 207],
  "searchContextId": "abc123...",
  "useSynonyms": true,
  "useIntent": true
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `query` | 是 | 用户问题 |
| `paths` | 否 | 指定文档路径列表；为空时自动检索选 topK |
| `chunkIds` | 否 | `/search/` 返回的切片 ID；用于定位所属最深章节，不直接把摘要或单个切片作为回答证据 |
| `searchContextId` | 否 | `/search/` 返回的短期上下文 ID，用于复用查询意图和改写结果 |
| `maxDocs` | 否 | 拼接给 LLM 的文档上限，默认 8 |
| 其他字段 | 否 | 同 `/search/` |

返回（精简）：

```json
{
  "query": "上市公司再融资中认购邀请书的作用是什么？",
  "answer": "根据《上海证券交易所上市公司证券发行与承销业务实施细则》第三十三条……[1][2]",
  "model": "deepseek-v4-flash",
  "llmConfigured": true,
  "usage": { "prompt_tokens": 1234, "completion_tokens": 567 },
  "documents": [
    { "path": "raw/sources/xxx.docx", "chunkId": 183, "chunkIds": [183, 194], "sectionId": "nod_...", "headingPath": "第三章 > 第五节", "complete": true, "chars": 3560 }
  ],
  "targetedEvidence": [],
  "searchResults": [ ... ],
  "synonymExpansion": { ... }
}
```

`answer` 中的 `[1]`、`[2]` 编号对应 `documents` 中 V7 完整章节证据的顺序。同一章节命中的多个 chunk 会合并为一份证据。

未配置 `LLM_API_KEY` 时：

```json
{
  "llmConfigured": false,
  "answer": "未启用可用 LLM，已按当前选择的证据文档直接拼接如下。……",
  "prompt": "用户问题：...\n\n原文证据：...",
  "context": "...",
  "model": "gpt-4.1-mini"
}
```

## 领域知识层（登录后 GET，写入需管理员）

每个项目在 `backend/data/domain_layers/{project_hash}/` 下生成同义词文件：

```
synonym.yaml   # 同义词
```

### GET / PUT /api/projects/{project_id}/domain-layer/synonyms/

GET 返回：

```json
{
  "synonyms": { "登录": ["登陆", "认证入口"] },
  "yaml": "登录:\n- 登陆\n- 认证入口\n"
}
```

PUT 请求体（二选一）：

```json
{ "yaml": "登录:\n- 登陆\n" }
```

```json
{ "synonyms": { "登录": ["登陆"] } }
```

### POST /api/projects/{project_id}/domain-layer/synonyms/import/

导入同类词并合并到现有 synonym。

请求体：

```json
{
  "text": "撮合:\n- 竞价\n- 集合竞价\n\n接口: API, HTTP接口",
  "merge": true
}
```

返回合并后的 `synonyms`。

## 知识摄入（仅管理员）

未登录调用返回 401；已登录的非管理员调用返回 403：

```json
{ "error": "需要管理员权限", "needLogin": true, "needAdmin": true }
```

### DELETE /api/projects/

移除本地项目登记（磁盘文件不删，仅管理员）。

```json
{ "id": "2886e610-7220-48ea-add6-f11c6d6a9fad" }
```

### POST /api/projects/register/

登记本地原文项目并建立 V7 文档、章节树和 FTS 索引，不调用 LLM。

请求体：

```json
{
  "projectDir": "D:\\KnowledgeProjects\\rules",
  "rawDir": "",
  "exportDir": "",
  "projectName": "法规知识库"
}
```

返回：

```json
{
  "ok": true,
  "registeredProject": { "id": "2886e610-...", "name": "法规知识库", "path": "..." },
  "projectDir": "...",
  "rawDir": "...",
  "exportDir": "...",
  "summary": {
    "sources": 12,
    "objects": 38,
    "hasIndex": true,
    "hasLog": true
  }
}
```

### POST /api/ingestion/build/

异步解析原文并直接写入 V7 内容库。子进程产生的一次性摄入载荷在原子索引提交后删除；任务记录保存在 Django 数据库。

历史任务通过 `GET /api/ingestion/jobs/?limit=100` 查询；完整日志通过 `GET /api/ingestion/jobs/<job_id>/` 查询。两个接口都仅限管理员使用。

不同项目的任务可以并行；相同项目或相同 `exportDir` 同时提交返回 HTTP 409。模型 HTTP 429 会产生 `rate_limited` 状态；`POST /api/ingestion/jobs/<job_id>/retry/` 创建新尝试并把并发数减半，最低为 1。

请求体：

```json
{
  "projectDir": "D:\\KnowledgeProjects\\rules",
  "rawDir": "",
  "exportDir": "",
  "limitDocs": 0,
  "llmConcurrency": 4,
  "maxItemsPerChunk": 10,
  "maxObjects": 160,
  "allowHeuristic": false,
  "projectName": "法规知识库"
}
```

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `projectDir` | 是 | 本地项目根目录 |
| `rawDir` | 否 | 默认 `项目目录/raw/sources` |
| `exportDir` | 否 | 只读 Markdown 导出目录，默认 `项目目录/exports/wiki` |
| `limitDocs` | 否 | 仅处理前 N 个文档，0 = 全部 |
| `llmConcurrency` | 否 | 单任务并行调用 LLM 的标题块数量，默认读取 `LLM_CONCURRENCY` 或使用 4；多任务时总请求并发约为各任务并发数之和 |
| `maxItemsPerChunk` | 否 | 单切片保留的知识对象总数，最大且默认均为 10；提示模型优先提取 3–5 个 |
| `maxObjects` | 否 | 按来源覆盖度、出现次数和说明完整度全局排序后保留的知识对象总数，默认 160 |
| `allowHeuristic` | 否 | LLM 失败时是否降级为规则抽取 |
| `projectName` | 否 | 列表中显示名 |
| `timeoutSeconds` | 否 | 默认 7200；完整章节处理可能需要较长时间 |

返回任务对象：

```json
{
  "id": "abc123...",
  "status": "running",
  "ok": null,
  "startedAt": 1740000000.0,
  "finishedAt": null,
  "progress": { "current": 0, "total": 0, "percent": 0 },
  "projectDir": "...",
  "rawDir": "...",
  "exportDir": "...",
  "summary": {
    "sources": 0,
    "objects": 0,
    "hasIndex": false,
    "hasLog": false
  },
  "logs": [],
  "error": "",
  "command": [ ... ],
  "timeoutSeconds": 7200,
  "projectName": "法规知识库"
}
```

### GET /api/ingestion/jobs/{job_id}/

轮询生成任务进度。

```json
{
  "id": "abc123...",
  "status": "running",          // running / succeeded / failed / rate_limited
  "ok": true,
  "progress": { "current": 12, "total": 30, "percent": 40, "unit": "chunks" },
  "logs": [ "[块 12/30] xxx.docx · 竞价交易 > 订单确认 完成", "PROGRESS chunks 12 30", ... ],
  "summary": {
    "sources": 12,
    "concepts": 30,
    "entities": 8,
    "keywords": 5,
    "hasIndex": true,
    "hasLog": true
  },
  "finishedAt": null,
  "registeredProject": null
}
```

`status` 为 `succeeded` 时会带 `registeredProject`（自动登记到项目列表）。进度只解析机器标记 `PROGRESS chunks <current> <total>`；文档和块日志中的展示编号不会被误判为整体进度。

DOCX 按 Word 大纲/标题样式建树，保留段落、列表和表格文本语义，并仅在最深章节内切片。对象必须可独立解释；关系只接受 LLM 明确输出的带类型三元组。

### GET/POST /api/projects/{project_id}/search-index/

`GET` 校验本地项目索引并返回 V7 版本、DocumentVersion、StructuralNode、chunk、知识对象、词汇、Claim 和 ClaimEvidence 统计。管理员使用 `POST` 更新原文索引；该操作不调用 LLM，重新抽取知识应使用摄入任务。V6→V7 直接重建，不迁移旧知识数据。

### GET /api/projects/{project_id}/index-inspector/

管理员只读检查原文解析和知识抽取结果。无参数时返回文档列表；传 `documentId` 返回清理后的解析文本、章节结构和全部 chunks；传 `chunkId` 返回原始 chunk、相邻 chunk、对象提及、Claims 以及最终提交给问答模型的“完整章节 + 上级公共内容”。该接口不重新解析文件，也不调用 LLM。

### POST /api/projects/{project_id}/export/

管理员从当前 V7 内容库重建只读 Markdown 到项目 `exportDir`。导出会覆盖系统生成的 Markdown，并删除遗留 `structure.json`；不会修改原文或内容数据库。

## 调用示例

### curl

```bash
# 1. 登录并保存 cookie
curl -c cookies.txt -X POST http://127.0.0.1:8091/api/auth/login/ \
  -H "Content-Type: application/json" \
  -d '{"username":"admin","password":"your-pass"}'

# 2. 检索问答（需使用登录 cookie）
curl -X POST "http://127.0.0.1:8091/api/projects/2886e610-7220-48ea-add6-f11c6d6a9fad/compose/" \
  -H "Content-Type: application/json" \
  -d '{"query":"认购邀请书的作用","limit":6}'

# 3. 管理员：启动知识摄入（需携带 cookie）
curl -b cookies.txt -X POST http://127.0.0.1:8091/api/ingestion/build/ \
  -H "Content-Type: application/json" \
  -d '{"projectDir":"D:\\LLMWikiProjects\\test","projectName":"测试库"}'

# 4. 轮询任务进度
curl -b cookies.txt "http://127.0.0.1:8091/api/ingestion/jobs/abc123.../"

# 5. 登出
curl -b cookies.txt -X POST http://127.0.0.1:8091/api/auth/logout/
```

### Python (requests)

```python
import requests

base = "http://127.0.0.1:8091"
s = requests.Session()

# 登录（管理员）
r = s.post(f"{base}/api/auth/login/", json={"username": "admin", "password": "your-pass"})
print(r.json())

# 检索问答
r = s.post(f"{base}/api/projects/2886e610-7220-48ea-add6-f11c6d6a9fad/compose/",
           json={"query": "认购邀请书的作用", "limit": 6})
data = r.json()
print(data["answer"])
print("证据文档:", [d["path"] for d in data.get("targetedEvidence", [])])

# 登出
s.post(f"{base}/api/auth/logout/")
```

## 错误返回格式

所有接口错误统一为 JSON：

```json
{ "error": "错误描述", "detail": "可选详情" }
```

常见状态码：

- `400` — 请求参数缺失或非法
- `401` — 登录失败、未登录或会话已失效
- `403` — 需要管理员权限
- `404` — 任务不存在
- `500` — 服务器内部异常
- `502` — 配置的 LLM 接口请求失败


## 文档版本范围

`GET /api/projects/{projectId}/document-versions/` 返回逻辑文档分组、各版 `editionId`、`versionLabel`、`path`、`sourceRef`、`chunkCount`、默认版本状态以及可选日期。`editionId` 是持久业务版本标识，区别于索引内部的 `documentVersionId`。

管理员 `POST` 同一路径：

- `{ "action": "initialize" }`：初始化／同步，旧 V7 索引首次迁移前保留 `.index/search.db.pre-versions.bak`。
- `{ "editionId": "...", "versionLabel": "0314", "documentName": "接口文档", "logicalDocumentId": "...", "setDefault": true, "publishedAt": "2026-03-14", "effectiveAt": "" }`：编辑版本、关联已有文档或指定默认版。除 `editionId` 外字段可省略。标签在同一文档内唯一，日期为可选 YYYY-MM-DD，空版本不能手动设为默认版。

`/search/`、`/compose/`、`/compose-stream/` 的请求支持 `versionScope: { "逻辑文档ID": "editionId" }`。省略项使用各文档默认版本。指定不存在或不属于该文档的版本返回 400，不降级到其他版本。图谱 GET 支持同名 JSON 查询参数。

搜索结果和回答证据包含 `editionId`、`logicalDocumentId`、`documentName`、`versionLabel`；预览历史原文使用 `/file-content/?path=...&editionId=...`，可附加 `structuralNodeId`。默认预览仅允许默认版本。版本范围变化或默认版本切换导致旧搜索上下文失效时返回 400，要求重新检索。流式回答在开启 SSE 前验证证据范围。

持久原文和目录清单位于项目 `document-history/`，应与项目一起备份；删除搜索索引后可从快照重建版本和标签，知识抽取需重新摄入。该目录不参与原文目录扫描。全量和单文档摄入均使用暂存索引，校验原文内容与发布基线后发布。


## 2026-10-26：上传摄入与项目登记

- `POST /api/ingestion/upload/`（管理员，multipart）：多份 `files`，`paths` 为与文件顺序相同的相对路径 JSON 数组。已有项目传 `projectId`；新项目传 `projectName`。继续给待摄入的新项目追加目录时传上次响应 target.path 作为 `projectDir`。返回待摄入 files、可选逻辑文档 documents 与服务端 target。文件必须为支持类型，路径不得绝对、越级或指向 Windows 设备名；最多1000份／512MB。不会覆盖原文件，不触发模型或发布索引。
- `POST /api/ingestion/sources/`：传 `projectId` 可读取服务端已登记项目的原文目录。每份文件返回 `status: new|changed|indexed`，界面据此默认勾选新增／变化文件。相对路径 `sourceFile` 用于后续摄入，`displayName` 用于界面展示。
- `/api/ingestion/build/` 保留既有性能、模型与 promptProfile 参数，并接收 `sourceFiles` 和 `sourceAssignments`。新界面始终提交明确选中的文件，避免隐式全量重做；归属配置随任务保存，成功后在暂存索引内原子发布，重试保留。
- `PATCH /api/projects/`（管理员）：`{"id":"项目UUID","name":"新显示名称"}`。仅修改显示名。
- `DELETE /api/projects/` 为移除登记，保留目录与关联历史，可再次登记恢复；运行中的摄入／评测仍阻止移除。
- `POST /api/projects/register/` 登记已有项目目录，保留已存在的原文目录配置和显示名（除非显式提供），不构建索引或调用模型。


### 2026-10-27 上传路径与大目录兼容

`POST /api/ingestion/upload/` 支持 `uploadSessionId`（32位UUID十六进制或标准UUID）。新项目首次请求未收到响应时，重试同一会话会复用存储位置。已有项目旧目录不存在时，响应 `target` 返回当前服务器的新存储路径和 `storageNotice`，不会在上传时修改 Project。

摄入请求带 `targetProjectId`，服务端保存原项目路径并把任务绑定到原项目ID。索引发布成功后，才原子更新登记位置与原文根；失败不创建重复项目或提前迁移登记。

`POST /api/ingestion/sources/` 支持 `{ "mode": "server", "sourceDir": "服务端文档目录", "projectId": "可选已有项目ID", "projectName": "可选名称" }`。原文根就是 sourceDir，递归保留相对层级，新项目在受管目录存索引，原文保持原位。

前端将目录拆为小批 multipart 请求，沿用原 files/paths 格式；路径去掉选择目录本身的一层根名称，与本地目录相对路径一致。1000份／512MB是整次选择上限，不要求代理一次接收全部文件。
