# 知识摄入性能修复（2026-10-26）

## 已核实的问题

当前源码每个约 1200 字符的标准 Chunk 都独立调用模型；完成进度统计的是 Chunk 数，不是文件体积。200MB 文件大小无法直接换算正文 token 数或完成时间。

本机历史任务 `a8e21da0` 在 1234 秒内完成 31/39 块，最后保存的日志包含 JSON 修复请求运行到 633 秒的记录。这证明二次生成存在长尾，但不是内网这批 120 个文档的实测。

同步日志处理原先每行都读取任务、查询项目、重写任务载荷。流式接收期间每 40 段正文打印一次日志；慢磁盘可能进一步阻塞子进程输出。

## 日志专项复测

2026-10-26 在本机临时数据库调用实际 `append_job_log` / `save_job`，预填 500 行日志，使用与当前业务库一致的 DELETE 日志模式和 FULL 同步级别：

| 路径 | 300 行总耗时 | 数据库保存次数 |
|---|---:|---:|
| 原逐行读取并保存 | 2.3852 秒（7.95ms/行，P95 9.773ms） | 300 |
| 内存累积后保存 | 0.0075 秒 | 1 |

这是同一短批次的日志处理对比，不是摄入整体加速倍数。原路径按每块 27 行估算，600 块约消耗 129 秒；旧记录即便按 51ms/行估算也约 826 秒，单凭这些数字不能解释 5 小时只完成 1%。内网若单次写入显著更慢，日志反压仍可能成为主瓶颈，需要在目标机器测量，不能用本机结果代替。历史任务中的数百秒模型调用长尾仍需排查。

## 本次改动

- 日志在工作线程内累积，每秒最多周期保存一次，结束和失败时保存最终日志与进度。
- 新任务默认 compact：摘要不超过 80 字、对象说明不超过 60 字，减少索引描述的生成量。原文和引用不变，但应验证知识对象召回质量。
- 新任务可在页面选择 standard；旧任务通过“重试”自动保留 standard，兼容旧断点。两种模式的缓存隔离。
- 每个工作线程复用 HTTP Session，响应成功或失败均关闭。
- 合法字符串以外的尾逗号在本地去除；其他语法错误仍最多调用一次模型修复。截断数据不会通过补括号伪装成完整结果。
- 单次输出默认最多 4096 tokens。达到上限明确失败，不缓存残缺抽取。
- `INGEST_THINKING=auto`（默认）在知识抽取时对 `deepseek-v4*` / `deepseek-flash` 显式发送 `thinking.type=disabled`，避免思考耗尽预算而没有抽取 JSON；不改变问答请求或其他模型。兼容该参数的服务可设 `enabled` / `disabled` 覆盖默认策略；请求日志的 `requestThinking` 记录实际设置。
- 输出超限会在任务详情显示具体原因，区分“仅返回思考内容”和正文截断；已失败任务可以直接重试，复用有效断点，无需重新上传。
- 流式事件到达时检查累计耗时，默认 180 秒（包含排队、思考和正文生成）；无数据时仍由 LLM_TIMEOUT 控制读取等待。这不是独立定时器式的硬截止。
- 流式日志降为每 10 秒心跳，记录请求总耗时和输出字符数；只产生思考内容时也检查耗时。
- 等待队列最多保留并发数两倍的任务，完成即回收，避免一次提交全部 Chunk 的 Future。

## 内网验证

部署更新并重启后端，再运行新任务；正在运行的旧子进程不会自动采用新代码。已有失败任务可用“重试”继续，保留已完成断点。

配置见 `backend/.env.example`：`INGEST_PROMPT_PROFILE`、`INGEST_MAX_TOKENS`、`INGEST_REQUEST_TIMEOUT`。默认并发仍为 4；内网模型允许时可在页面逐步试 8，观察总完成块数/秒、限流和单请求耗时再调整。250–300 tokens/s 是单请求还是整台服务的总吞吐需要分别确认，并发不会自动带来线性加速。

验证日志中的总块数、每次 LLM 完成耗时、字符数和 JSON 修复次数。用相同文档和模型比较完成块数/分钟；当前没有连接内网模型，也没有据此宣称固定提速倍数。

运行检查：后端 `python manage.py test api.tests.test_ingestion_performance api.tests.test_wiki_builder api.tests.test_wiki_jobs`；前端 `npm run build`。

## 内网性能日志使用说明

默认启用，无需额外开关。部署并重启后端后，新启动或重试的任务会生成：

`<项目目录>/.index/ingestion-<任务ID>.perf.jsonl`

这是每行一条 JSON 的完整性能记录，任务成功或失败后保留。任务详情接口的 `performanceLog` 是文件位置，`performance` 是累计汇总；页面任务日志可以看到 `PERF` 行，但只保留最近 500 行。直接运行摄入脚本时，`PERF` 行输出到终端；独立文件由后台任务的父进程保存。

| 记录/字段 | 用途 |
|---|---|
| `index_start` / `index_ready.seconds` | 结构索引准备耗时，包含解析、切片、FTS、可能的快照复制与缓存读取，不是纯解析时间 |
| `index_ready.chunks` / `inputChars` / `sourceBytes` | 实际任务规模；字符量包含切片重叠 |
| `llm_request.firstEventSeconds` | 从请求发起到首个可解析 SSE 事件，包含连接、服务端排队等，不能单独分离服务端排队 |
| `llm_request.firstContentSeconds` | 到首段正文的等待；思考事件不算正文；非流式返回为空 |
| `llm_request.seconds` / `outputChars` / `reasoningChars` | 每次模型调用总耗时、正文和思考字符量，失败请求也记录；不保存文本内容 |
| `completionTokens` / `completionTokensPerSecond` | 仅在服务端返回 usage 时可用；速率为 token 数除以完整请求时长，包含等待，不是纯解码速度；缺失为 null |
| `outputCharsPerSecond` | 字符数除以请求总耗时，不等于 tokens/s |
| `json_repair` | 每次发起模型 JSON 修复；汇总 `jsonRepairRequests` 是修复请求数，不是日志行数 |
| `extraction_progress` / `extraction_end` | 已完成块数、复用断点数、并发数、在途/等待任务数、每分钟新完成块数 |
| `merge_payload_end` / `publish_end` / `export_end` | 结果合并、索引发布、Markdown 导出耗时 |
| `parent_summary.periodicLogSaveSeconds` / `periodicLogSaveMaxMs` | 父进程周期性任务日志写库累计耗时和单次最大毫秒数 |
| `parent_summary.periodicLogSaveWallPercent` | 周期性日志写库耗时占任务墙钟时间百分比；不包含最终状态保存及性能文件写入 |
| `pipeLagSeconds` / `maxPipeLagSeconds` | 子进程发出性能事件到父进程接收的延迟及最大值，用于发现输出管道积压；系统校时也可能影响该值 |
| `sqliteJournalMode` / `sqliteSynchronous` | 实际任务库 SQLite 日志模式及同步级别 |

模型接收心跳、抽取进度和父进程汇总最多约每 10 秒记录一次（依赖事件到达）；每次请求结束另记一条。性能文件缓冲写入，并随每秒周期保存刷新，避免每个 token 写磁盘；结束时刷新。文件写入失败时任务继续，页面日志会提示，数据库仍保存汇总。

排查顺序：先看 `periodicLogSaveWallPercent` 与 `maxPipeLagSeconds`，判断写库是否挤占主循环并导致积压；再看单请求首事件/首正文时间、总耗时、JSON 修复数；最后比较结构准备、发布、导出的阶段耗时。并发请求的时长不能相加后当成任务墙钟时间。

请保留完整 `.perf.jsonl` 文件用于分析。任务内有文档名和结构路径，但性能记录不包含 API Key、提示词、模型正文或思考正文。

## 无正文、持续事件直到超时的排查

如果 `events` 持续增长，但 `outputChars=0`、`firstContentSeconds=null`，不能把事件数当作生成 token 数。`reasoningChars=0` 也只表示没有识别到 `delta.reasoning_content`，不能证明服务端没有思考。约 181 秒的 `TimeoutError` 是客户端 180 秒保护触发，不是服务端正常完成。

新增 `llm_event_shape`：每个请求最多记录 4 种不同的事件结构，仅包含字段名和字段类型，不含字段值。`httpStatus`、`contentType`、`emptyChoiceEvents`、`emptyDeltaEvents`、`serverErrorEvents` 与结构样本一起保存在最终 `llm_request`。HTTP 200 中嵌入的 error 事件会立即报错，避免继续等待到总超时。

内网无法导出文件时，部署更新后先用单文档、并发 1 做小规模验证，拍摄 `llm_event_shape` 和该请求最终的 `llm_request` 即可。若要验证写库瓶颈，再拍摄一条 `parent_summary`。不要仅提高超时或提高并发来处理这种没有正文的情况。

## 请求与流式接收代码核查

摄入发送到 `<apiBase>/chat/completions` 的请求包括 `model`、system/user `messages`、`temperature=0`、`max_tokens=4096`（可配置）、`stream=true`（可配置）。当前没有发送思考开关，沿用服务默认行为；DSH 的具体采样参数不在本仓库内，不能声称与 DSH 的完整请求体完全一致。

已在本地 HTTP 服务复现接收端缺陷：服务发送首段 SSE 正文并保持响应未结束，旧 `iter_content(chunk_size=None)` 在带 Content-Length 的非 chunked 响应上直到响应结束才交付数据。改为 urllib3 的 `read1` 及时读取可用字节，旧版本无该方法时退回单字节读取；仍然按完整行解码 UTF-8。真实 HTTP 测试覆盖 Content-Length、连接关闭定界、chunked 三种传输，以及各自的 gzip / 无压缩情况，验证正文在响应结束前可读。这个复现不能单独证明它就是内网截图的全部原因。

摄入流式请求增加 `Accept-Encoding: identity`，避免主动协商压缩导致额外缓冲。新增请求模型、温度、token 上限、是否流式、环境代理是否启用的指标，以及 `headersSeconds`（等待 HTTP 响应头的时间）、`transferEncoding`、`contentEncoding`、`contentLength`。若 headers 很快但 firstEvent 明显更慢，应继续检查响应体传输、网关缓冲及服务端发出事件的时间；这些客户端时间不能单独分离服务端排队。

部署时需要包含 `backend/api/llm.py` 和 `backend/tools/ingest_project.py`，并保留此前父进程性能日志改动；重启后再运行新任务或重试。该接收函数也被网页流式回答复用。

## 内网截图确认的字段差异（2026-10-28）

内网模型返回 `delta.reasoning`；此前只统计 `delta.reasoning_content`，造成 `reasoningChars=0` 和大量 `emptyDeltaEvents` 的误导。现统一兼容这两个文本字段，思考不混入抽取 JSON；非流式和网页思考显示也使用相同规则。结构诊断同时记录思考字段类型。新增 `finishReason`，可辨认正常结束、token 预算耗尽及其他终止原因。

本次截图中响应头仅等待约 0.02–0.045 秒、首事件约 0.875 秒，且为 chunked、无压缩、无环境代理；一块成功请求约 4.2 秒完成。因此不能将这批所有慢请求归因于连接慢或所有响应都被整段缓冲。慢请求携带 reasoning 字段是当前首要线索，但字段存在不等于已知非空思考量，需要修正后的计数验证。`requestThinking=null` 表示未显式发送思考开关，不表示禁用了思考；模型内部别名未必能命中 auto 模式的模型名称判断。

无法导出日志时，优先搜索慢块对应的最终 `llm_request`（例如同一个“块 10/14175”或 requestId），拍摄 `seconds`、`ok`、`errorType`、`finishReason`、`reasoningChars`、`outputChars`。然后在 `.perf.jsonl` 文件中搜索 `parent_summary`，拍摄 `periodicLogSaveWallPercent`、`periodicLogSaveMaxMs`、`maxPipeLagSeconds`。无需拍摄完整请求正文或密钥。
