# 摄入批量抽取、失败处理与提示词管理

## 分阶段转换与留档

先完成所有文档的 Markdown 转换，再构建私有索引、抽取知识和发布成功部分。中间产物保存在 `<项目>/.index/ingestion-artifacts/<checkpointId>/`：

- `markdown/`：转换成功的 Markdown，文件名包含原文件名及路径摘要，任务成功后仍保留。
- `manifest.json`：原文路径、哈希、Markdown 路径及逐文档结果。失败状态为 `conversion_failed`、`index_failed`、`extraction_failed`，含错误原因。

部分成功可在任务历史点击“重试”，复用内容未变化的转换缓存及有效抽取断点；全部成功后只清理抽取断点，Markdown 留档不删除。同任务重试会更新 manifest。失败的本次文档版本不会进入检索；范围外文档与未参与此次导入的历史版本保留。普通手动重建索引仍保持失败回滚规则。

文档转换超时在 `backend/.env` 配置 `DOCUMENT_CONVERSION_TIMEOUT=120`（秒），并行转换配置 `DOCUMENT_CONVERSION_CONCURRENCY=2`；修改后重启后端。这与模型请求超时不同。

## 默认配置

```ini
INGEST_BATCH_CHARS=3000
INGEST_BATCH_SIZE=6
INGEST_FIRST_CONTENT_TIMEOUT=45
INGEST_REQUEST_TIMEOUT=90
INGEST_RETRIES=1
```

将这些值加入内网 `backend/.env`，替换已有同名设置，并重启后端。并发保留 4；更换模型后先以单文档、并发 1 验证，确认服务端容量后再增加。代码不更换当前模型，也不假设上游一定支持关闭思考。

管理控制台 → 知识摄入可设置“每次抽取最多正文字符”和“每次抽取最多原文块”；API 对应 `extractionBatchChars`、`extractionBatchSize`，CLI 对应 `--extraction-batch-chars`、`--extraction-batch-size`。最大范围分别为 500–12000、1–12。将块数设为 1 可禁用批量。

这里调整的是**一次模型请求的输入批次**，不改变既有检索 Chunk、章节树、版本和证据编号。分组仅限同一文档，按原顺序累积；预算保守计入标题和编号开销。原始单块若本身超过预算仍独立完整发送，不截断正文。

批量请求按每块 chunkId 分别返回摘要、对象和关系。程序验证编号无遗漏、无重复、无额外编号，且对象与关系为数组；再按编号写入各自断点。批量响应错误、超长或重试耗尽时拆回单块处理。跨块关系不作推断，不把批次摘要复制到每个切片。

任务进度仍按原始 Chunk 数显示，因此原来的 14175 块不会在页面变成几千块；减少的是模型请求次数。实际收益取决于模型批量输出能力和重试率，不宣称固定倍数。

## 超时与失败

- 流式首正文 45 秒截止：收到思考或心跳也不会重置。正文一旦出现，首正文计时停止。
- 单次请求总截止 90 秒：正文或思考持续流入也不能无限占用槽位。截止后主动关闭已取得的连接；请求头等待另有连接与读取超时，DNS/底层网络异常仍可能受到操作系统行为影响。
- 超时、连接错误和 HTTP 5xx 默认自动重试一次，退避 2 秒；配置最多允许 3 次重试。输出预算耗尽不盲目重复相同请求。批次拆单是额外的降级步骤，因此一批最多请求次数不等于“重试数 + 1”。
- 普通单块错误不终止兄弟块，成功结果立即保存。限流、取消、明确的 HTTP 400/401/403/404 配置或鉴权错误以及断点写盘失败会中断任务。致命退出时停止排队、关闭活动连接；CLI 使用非零退出码避免解释器等待尚未取得响应头的工作线程。
- 全部块处理完后按文档判定完整性：任何块失败，该文档整体从本次待发布索引排除；其他完整文档正常发布，任务显示“部分成功”，保留断点供重试。若所有文档均失败，则不发布空索引，原线上数据保持原样。鉴权、限流、磁盘等全局错误仍会中断任务。

失败记录在 `<项目>/.index/ingestion-checkpoints/<checkpointId>/failures/<key>.json`，任务结束时另写 `failures.json` 汇总。成功重试会移除对应单块失败文件。进程异常退出时可通过已落盘的单块失败文件排查；尚在进行的块没有成功断点，重试会重新处理。

## 提示词管理

管理控制台 → **提示词管理** 可编辑系统提示词、精简抽取模板、标准抽取模板。默认规则增加“分类不确定立即使用 other/unspecified，不反复比较标签；缺乏实质内容的标题返回空数组；不要重复检查自己”。这些提示不能保证强制思考模型停止思考，仍应使用可靠的非思考模型或上游实际支持的思考开关。

正文模板变量：`${source_name}`、`${heading_path}`、`${section_text}`、`${output_schema}`。后两个必须保留；JSON 中的花括号按原样填写，普通美元符号写成 `$$`。系统提示词为纯文本。

模板存储在 `backend/data/ingestion_prompts.json`。仅管理员能读取或修改；错误模板不会覆盖现有设置。每个任务在断点目录保存 `prompts.json` 快照，新任务使用最新模板，运行中或同任务重试使用原快照。要试新模板应新建任务，而不是重试旧任务。

提示词指纹进入断点键，避免把旧提示词的答案当成新提示词结果。**首次升级到本版后，旧格式的抽取断点不命中，需要重新抽取；无需重建已有检索索引。** 此后同任务失败重试正常复用。不要删除仍需重试的断点目录。

## 内网验证与部署

需部署完整更新后的后端和前端构建，包含新增的 `api/ingestion_prompts.py` 和提示词管理页面；无需数据库迁移。保留内网 `.env`、数据库、上传原文及项目索引。重启后先跑一个文档，确认 `extraction_settings` 中批次大小、超时、重试数和提示词指纹正确。

重点日志关键词：`extraction_settings`、`chunk_retry`、`batch_fallback`、`chunk_failed`、`llm_request`、`parent_summary`。汇总额外统计 `failedChunks`、`retryAttempts`、`batchFallbacks`。`reasoning` 与 `reasoning_content` 均计入思考字符，不当作正文或空事件。性能日志不保存原文、模型正文或思考正文。

本地验证包含：真实 HTTP 持续思考流被首正文计时器中止、主动取消关闭连接、批量编号乱序映射/漏号降级、单块失败后兄弟块继续以及重试只处理失败块、提示词权限/校验/快照隔离。内网实际吞吐仍需用更换后的模型验证。

## Windows 长路径与 MarkItDown 转换修复

本次反馈中的模型请求以约 240 tokens/s 正常完成，任务随后因断点临时文件路径达到 261 字符而失败。现已将断点、清单、载荷和提示词快照的文件操作统一接入 Windows 长路径支持，并缩短断点临时文件名。无需为这个错误更换模型或删除原文；部署后重启后端，再重试任务。

`couldn't find ffmpeg or avconv` 来自 MarkItDown 可选音频模块的导入警告；当前支持的 DOCX、PDF、Excel、PPTX 转换不会调用音频转换器。不必仅为此安装 ffmpeg。已限定抑制这一条无关警告，其他依赖和转换异常继续报告。

已知文件扩展名直接选择 MarkItDown 对应转换器，省去每个文件重新构建 Magika/ONNX 格式识别会话。二进制文档使用独立转换进程，默认 `DOCUMENT_CONVERSION_TIMEOUT=120` 秒；超时杀掉进程并报告文件名。较大的合法 PDF 若确实需要更久，可按实测调大该值。这增加了每个二进制文件一次 Python 进程启动开销，换取转换期限可控，不宣称所有文件都提速。

新增 `CONVERT` 日志：`source_start` 给出原始文档路径，`start` / `done` / `timeout` / `failed` 记录实际转换文件、耗时和结果。原始文件可能转换自哈希命名的版本快照，因此结合前一条 source_start 识别原文。后台任务的完整性能文件中，对应阶段以 `conversion_` 开头保存。

部署必须包含新增 `backend/tools/convert_document.py`、更新后的 `api/document_conversion.py`、`api/file_paths.py`、`api/ingestion_prompts.py`、`api/search_index.py`、`api/views.py` 和 `tools/ingest_project.py`。不只是刷新浏览器，需要重启后端让父进程和新子进程都加载更新；不需要迁移数据库。解析器版本已更新，之后重新摄入所选文件时会使用新转换方式；已发布索引不会因升级自动重建。

## 批量转换进度与并行解析（2026-10-29）

```ini
DOCUMENT_CONVERSION_CONCURRENCY=2
DOCUMENT_CONVERSION_TIMEOUT=120
```

转换并发与模型并发是独立设置。默认并行解析 2 个文件，最大允许 4 个，内存紧张时设为 1。只有文件解析在工作线程进行；SQLite 写入留在主线程，并按原发现顺序入库，以保持版本和引用编号稳定。最多预取并发数两倍的文件结果，避免把整批解析结果同时留在内存。不保证内网 218 个文件固定在几分钟内处理完。

新任务在页面显示“本次原文解析 current/total、失败数、解析缓存复用数”；日志关键词：

- `conversion_batch_start`：在完整 `.perf.jsonl` 文件中查看本次文件数、并发、`converterVersion`、`parserVersion`，可确认后端是否加载新版。
- `conversion_file_start` / `conversion_file_done`：原始路径 `sourceRef`、文件序号、已完成数、耗时、`cached` 和正文字符数。
- `conversion_file_failed`：对应原始路径和底层错误。页面执行日志中原始阶段名为 `CONVERT file_start/file_done/file_failed`。

`FileConversionException` 只是封装错误，并不能证明文件损坏。转换子进程现在返回最长 6 层、每层最多 600 字符的异常链，包括 MarkItDown 的失败尝试；子进程尚未进入转换就崩溃时保留 stderr 末尾诊断。这样可区分缺依赖、BadZipFile、XML 格式错误等情况。诊断可能包含格式解析错误片段，只在管理员任务详情及项目性能文件中保留。

成功的解析结果保存到 `<项目>/.index/parsed-cache/`，按文件实际内容 SHA256、扩展名和解析器版本隔离；即使整批因少数文件失败未发布，重试也能复用健康文件的解析结果。文件内容或解析器版本变化会重新解析；全量强制重建绕过缓存。缓存仅保存解析结果，不等于模型抽取断点，不会把失败文件伪装为成功。可删除该目录清理缓存，下次会重新解析，不影响已发布索引与原文。

一个文件失败或超时仍继续处理其余文件，最后汇总所有失败；有失败时不发布缺文件索引，线上旧证据不变。请根据新日志修复或取消选择失败文件后重试。旧 `.doc`、WPS `.et` 仍明确列为不支持，不把这些二进制格式当作 PlainTextConverter 的文本输入；应先用 Office/WPS 另存为 `.docx` / `.xlsx`，不能只改文件后缀。

需同步部署后端和前端构建并重启服务，新进度显示才会生效。若仍只有 `FileConversionException` 而没有 `conversion_batch_start` 中的版本与逐文件诊断，先检查是否运行了旧目录、旧进程或旧任务。
