# 本地知识摄入与 Markdown 导出

本项目使用 Microsoft MarkItDown 将本地原文统一转换为 Markdown，再构建每项目 SQLite 内容库。转换结果参与章节解析、切片和知识抽取；`exports/wiki` 下的 Markdown 仍然是只读导出，不作为主数据。

## 输入

项目登记时指定一个 `rawDir`，支持 `.doc`、`.docx`、`.xlsx`、`.xls`、`.pptx`、`.pdf`、`.md`、`.txt`、`.csv`、`.html`、`.htm`。浏览器上传、目录扫描和命令行使用相同的支持范围。`项目目录/raw/sources` 只是默认值，不是强制目录协议。

安装：`backend/.venv/Scripts/python.exe -m pip install -r backend/requirements.txt`。固定使用 `markitdown[docx,xlsx,xls,pdf,pptx]==0.1.9`，仅启用对应格式的官方转换器，不调用云服务或模型，不自行实现 Office/PDF 解析器。摄入时损坏、加密或依赖缺失的文档会记录失败并跳过，仅发布其余完整文档。普通手动重建索引仍保持原有失败回滚规则。

Excel 各工作表转换为二级标题和 Markdown 表格，保留表头和数据。公式读取已保存的计算结果，不负责重新计算，导入前请在 Excel 中计算并保存。当前不包含扫描件 OCR；Markdown 不保证保留 Office 样式、图表和所有表格布局。

选型：MarkItDown 适合当前 Office/文本到 Markdown 的统一入口；Docling 也支持 Office 到 Markdown，并提供文档结构、PDF 版面分析和 OCR 流水线，如后续需要可评估替换转换入口。参考：[MarkItDown](https://github.com/microsoft/markitdown)、[Docling 支持格式](https://docling-project.github.io/docling/usage/supported_formats/)。

摄入流程：

```text
原文 → MarkItDown → Markdown → DocumentVersion → StructuralNode → RetrievalChunk/FTS
     → KnowledgeObject/ObjectTerm/ObjectMention
     → Claim/ClaimEvidence
```

LLM 直接处理 V7 已生成的 RetrievalChunk，不再自行解析或切分原文。每次模型请求最多保留 10 个高价值对象候选；项目级默认保留 160 个，可通过 `maxObjects` 调整。

关系只接受原文明示的 `source / relation / target`，可选限定字段为 `when`、`scope`、`effectiveFrom`、`effectiveTo`、`exception`、`note`。系统保存关系和证据，不进行规则推理。

## 后台任务

```http
POST /api/ingestion/build/
GET  /api/ingestion/jobs/
GET  /api/ingestion/jobs/{jobId}/
POST /api/ingestion/jobs/{jobId}/retry/
```

请求示例：

```json
{
  "projectDir": "D:/knowledge/example",
  "rawDir": "D:/knowledge/example/sources",
  "exportDir": "D:/knowledge/example/exports/wiki",
  "projectName": "规则知识库",
  "llmConcurrency": 4,
  "allowHeuristic": false
}
```

所有格式通过统一转换入口输出 Markdown，再按 Markdown 标题建立章节树，段落、列表和表格作为章节子节点；RetrievalChunk 只在所属最深章节内部切分。结构路径统一为“相对目录 > 文件名 > 文档章节”。抽取结果直接携带已有的 `chunkId`、`structuralNodeId` 和 `documentVersionId`，ObjectMention 与 ClaimEvidence 直接引用标准 Chunk，不再按文字反向寻找位置。校验成功后提交 V7 知识数据并删除一次性载荷。

失败任务的章节结果保存在 `.index/ingestion-checkpoints/<checkpointId>/`；重试只处理未完成的标准 Chunk，全部成功后清理检查点；部分成功保留断点供重试。转换得到的 Markdown 和逐文档失败清单另行长期保存在 `.index/ingestion-artifacts/<checkpointId>/markdown/` 与同级 `manifest.json`。

V6 升级到 V7 时直接从原文重建，不导入 V6 KnowledgeObject/Claim；需要在 V7 原文索引完成后重新运行知识摄入。

## Markdown 导出

默认导出到：

```text
exports/wiki/
  index.md
  log.md
  sources/
  concepts/
  entities/
```

`concepts/entities` 只为当前展示格式保留，不对应主数据表。系统不再生成 `structure.json`，也不会读取导出 Markdown 恢复知识。删除整个 `exports/wiki` 不影响构建、检索、回答或图谱。

## 索引维护

```powershell
cd backend
.\.venv\Scripts\python.exe manage.py build_search_index --project-id "项目UUID"
.\.venv\Scripts\python.exe manage.py build_search_index --project-id "项目UUID" --force
.\.venv\Scripts\python.exe manage.py verify_search_index --project-id "项目UUID"
```

普通索引维护只更新原文版本、结构节点、chunk 和 FTS，不调用 LLM。需要重新抽取知识对象和 Claim 时使用摄入任务。

转换器升级后解析版本随之更新，下次构建会重新解析原文。已有知识需要重新运行摄入后才使用新的切片和证据；升级本身不会自动改写现有项目。

## 旧格式与结果核对

XLS 使用 MarkItDown 官方 XlsConverter（xlrd）转换，无需 Excel。DOC 使用 LibreOffice 无界面转换为 DOCX，再交由 MarkItDown；服务端需要安装 LibreOffice，默认查找 PATH 和 Windows 标准安装目录，也可配置 `LIBREOFFICE_PATH`。缺少组件、超时、加密和损坏文件均按文件报告失败，不计为入库成功。

上传仅代表文件已接收。任务详情分别显示解析失败、索引失败、抽取失败、发布校验失败以及待完成数量，展开清单可查看全部文件的结果与原因。实际入库数在发布后读取数据库逐份核对；没有正文切片的文件不能计为成功。

统计区分文件版本数和所属文档数：例如 16 个文件版本归属 14 份文档时，文档列表显示 14 份，另外的版本从文档版本中查看。每份成功文件显示默认版状态和正文块数，报告保存在任务的 manifest.json 中。
