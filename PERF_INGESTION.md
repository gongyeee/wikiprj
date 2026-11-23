# 摄入性能瓶颈诊断（2026-10-23）

## 一句话结论

**慢的不是模型，也不是解析，是「块数 × 单块 LLM 成本 ÷ 并发」这个乘数结构，再加上两处纯浪费（日志逐行写库、JSON 修复二次调用）。**

按你现在 200MB 语料的规模，`600 块 / 6 小时` 不是「慢了 3 倍」，而是**总量的一小部分**——见第 3 节。

---

## 1. 实测数据（本机 SSD，非估算）

| 环节 | 实测值 | 判定 |
|---|---|---|
| docx 解析（`read_docx_document`） | **2823 KB/s** | 200MB 全部解析约 **72 秒**，不是瓶颈 |
| 切块（`split_document_chunks`） | 128 块 / 0.5 ms | 不是瓶颈 |
| 建结构节点 + chunk + FTS | 1592 段落逐条 INSERT | 单事务内，秒级，不是瓶颈 |
| `index_tokens`（5000 术语 × 每块） | 1 ms/块 | 不是瓶颈 |
| **任务日志逐行写库（改前）** | **51.00 ms/行** | **是瓶颈** |
| 任务日志逐行写库（改后） | **0.39 ms/行** | 已修复，**131×** |
| 单块 LLM 输出体积 | 1792 字符 / 5.3 对象 / 3.5 关系 ≈ **1200–1400 tokens** | **是瓶颈** |
| 单块 LLM 输入 | 最多 6000 字符 ≈ 4000 tokens | 次要 |
| 块密度 | 1200 字符/块，160 重叠（**冗余 10%**） | **是瓶颈** |

> 解析侧全部加起来 1–2 分钟。**排除解析、切分、SQLite 索引、FTS 的嫌疑。**

## 2. 历史任务实测对照

从 `api_ingestionjob` 表里取的三次真实运行：

| 任务 | 模型 | 并发 | 块数 | 耗时 | 每块墙钟 | 每调用耗时 | JSON 修复 |
|---|---|---|---|---|---|---|---|
| `ef14c607` | deepseek-flash | 4 | 293 | 30.8 min | 6.3 s | **25 s** | 0 次 |
| `a8e21da0` | Qwen3.8 Flash | 4 | 31/39 | 20.6 min（失败） | 40 s | **160 s** | 日志窗口内 224 行 |
| 你内网这次 | ? | 4 | ~600 | 6 h | **36 s** | **144 s** | ? |

两个致命差异：

1. **Qwen 那次几乎每块都触发 JSON 修复**（426 行日志里 224 行是「JSON修复」）。修复请求会把整个畸形响应再喂一遍，实测单次 **250–650 秒**。等于 **LLM 调用次数直接翻倍，而且翻倍的那一半更慢**。
2. 你内网单调用 144s ≈ 等效 **9.7 tokens/s**，而不是你以为的 30 t/s。差额被三件事吃掉：无连接复用（每块新建 TCP）、JSON 修复、以及 stdout 反压（见 4.1）。

## 3. 你的规模代入成本公式

```
总耗时 ≈ (正文总字符 / 块大小) × 单块LLM耗时 ÷ 并发  +  日志开销
```

本机实测 **docx 每 KB 磁盘体积产出 600 字符正文**（中文 UTF-8 + XML 标记，压缩比约 9×）：

| 项 | 数值 |
|---|---|
| 200 MB 原文 | ≈ **1.2 亿字符正文** |
| 1200 字符/块 | ≈ **15 万块** |
| 3000 字符/块 | ≈ **4–6 万块**（受章节边界封顶，见下） |

> 块大小提升的收益会饱和：块不能跨越章节，所以每文档的块数下限 ≈ 章节数。样本文档 62 个章节，1200 字符切出 128 块，3000 字符只降到 73 块。

**按当前 36 s/块推算：15 万块 ≈ 1500 小时 ≈ 62 天。** 6 小时做完 600 块 = 完成 **0.4%**。

这不是调参能救的，必须同时动块大小、输出体积、并发三个乘数。

## 4. 瓶颈清单（按影响排序）

### 4.1 【P0】日志逐行写库 —— 已修复

**现象**：`append_job_log` 每收到子进程一行 stdout，就 `SELECT job → JSON 解码 → SELECT project → SELECT job → UPDATE`，把最多 500 行日志的完整 payload 重写一遍。

**根因**：控制库 `db.sqlite3` 用的是 SQLite 默认 `journal_mode=DELETE` + `synchronous=FULL`，**每次 UPDATE 都要建日志文件 + fsync**。

**实测**：`DELETE/FULL = 51.14 ms` → `WAL/NORMAL = 0.24 ms`，**213×**。

**放大效应**：每块产生约 27 行日志（`正在接收` 每 40 个 SSE 事件打一行，一次 960 事件的响应就是 24 行）。600 块 = 1.6 万次 fsync = **826 秒纯写盘**。内网机器若有杀软/机械盘/网络盘，这项会到小时级。

**更隐蔽的伤害**：父线程是**串行**读 stdout 的。它卡在写库上时，Windows 匿名管道只有 4–8 KB 缓冲，**子进程的 `print` 会被阻塞**，而 `print` 就发生在 worker 线程的流式读取循环里——于是 LLM 的 TCP 接收窗口被撑满，服务端停止推送。**日志写库的延迟会直接变成 LLM 吞吐的上限。**

**已改**：
- `api/apps.py`：连接建立时统一 `PRAGMA journal_mode=WAL; synchronous=NORMAL; busy_timeout=20000`
- `api/views.py::append_job_log`：内存持有运行中 payload，改单条 `UPDATE`，不再走 ORM 读改写
- 日志窗口 500 → 200 行（payload 变小）
- `save_job` 缓存 project id，省掉每次一条 `SELECT`

### 4.2 【P0】单块 LLM 输出 1400 tokens —— 需你决策

1200 字符的输入，要求产出「300 字摘要 + 3–5 个对象（每个带完整说明、别名、关键词）+ 关系三元组」，**输出比输入还长**。这是在让模型把原文重写一遍。

- 未设 `max_tokens`：模型可以无限生成。日志里 250–650 秒的调用，按任何合理 token 速率都意味着**生成了一万多个 token**。
- 无 `response_format: json_object`：靠提示词约束 JSON，模型一跑偏就触发 4.3 的修复。

**已改**：加 `max_tokens`（默认 2000，`LLM_MAX_TOKENS` 可调）作为跑飞保护。**注意这是安全网不是加速器**——模型自然停就无影响，只有跑飞时才截断。

**待你决策**：把 prompt 收敛到「摘要 ≤150 字 + 2–4 个对象 + 关键词 ≤4」，输出可降到 500–600 tokens，**单块时间 ÷2.5**。代价：`EXTRACTION_PROMPT_VERSION` 必须改，**已完成的所有 checkpoint 全部作废**。你目前有 600 块缓存，是否接受由你定。

### 4.3 【P0】JSON 修复 = 第二次完整 LLM 调用 —— 已缓解

**现象**：Qwen 那次 50% 的日志行是「JSON修复」，实测修复调用 **250–650 秒/次**。

**根因**：`json.loads(clean_json_text(text))` 只做去围栏 + 截首尾大括号。模型输出尾部逗号、被截断、前后带寒暄，全部判失败 → 把整个畸形响应再发一次让模型修 → 修复请求同样没有 `max_tokens`，可以再跑十分钟。

**已改**：新增 `parse_json_lenient()` / `repair_json_text()`，**在进程内修掉机械性缺陷**（尾逗号、未闭合括号、截断、围栏、前后散文），修不动才走一次远程修复，且修复请求也带上 `max_tokens`。四个真实失败形态已单测覆盖，全部本地修好。

### 4.4 【P0】并发 4 太低 —— 已改默认 8

`LLM_CONCURRENCY` 默认 4。这是最直接的线性杠杆。已把默认提到 8；**建议直接上 16–24**（`LLM_CONCURRENCY=16`），前提是模型端能扛住。限流时重试逻辑会自动把并发减半，是安全的。

### 4.5 【P1】无 HTTP 连接复用 —— 已修复

`requests.post` 每块新建连接。内网代理每次请求都要付一次建连成本（实测本机代理返回一个 502 都要 2.05 秒，其中建连 0.8 ms、其余全是代理自身开销）。已改为 **thread-local `requests.Session` + 连接池**。

### 4.6 【P1】块太小 + 10% 重叠 —— 需你决策

`split_document_chunks(parsed, target_chars=1200, overlap_chars=160)` 是**硬编码**的，没有配置入口。1200 字符/块意味着 1.2 亿字符 → 15 万次 LLM 调用。

改成 3000 字符/块，块数降到 4–6 万（**÷2.6**），且重叠冗余从 10% 降到 1%。代价：`PARSER_VERSION` 必须 bump，**整个 V7 索引重建，现有 600 块也白做**。

**这是所有单点里收益最大的一个，但也是侵入性最强的。**

## 5. 另外两个必须知道的问题

### 5.1 Excel 文件被静默丢弃

```python
SUPPORTED_RAW_SUFFIXES = {".docx", ".pdf", ".md", ".txt", ".csv"}
```

**`.xlsx` / `.xls` / `.doc` 全部不在列表里。** `discover_documents` 和 `discover_project_documents` 用后缀过滤，命中不了就跳过，**不报错、不提示、不计入总数**。

你说「120 份 word/excel」，那 Excel 那部分**一块都没进库**，600 块全部来自 Word 子集。要么先转 docx，要么补解析器。建议至少在 `scan_ingestion_sources` 里把跳过的文件数打出来，别再静默。

### 5.2 新建导入任务不复用断点

```python
checkpoint_id = checkpoint_id or uuid.uuid4().hex
```

`retry_ingestion_job` 会传回旧 `checkpointId`（复用正常），但**从 UI 重新发起一次导入就是一个新 uuid，之前所有 checkpoint 全部找不到**。而且任务成功后 `shutil.rmtree(checkpoint_dir)` 把缓存删了。

对一个要跑几十小时的任务，这意味着：中途想改个参数重来，就得从头再跑一遍。建议把 checkpoint 目录改成按项目固定（`<project>/.index/checkpoints/`），key 里已经包含 `model + apiBase + promptVersion`，天然隔离，不需要每次新建目录。

## 6. 改完后的预期

| 杠杆 | 倍数 | 状态 |
|---|---|---|
| 日志写库 | ÷1.4（600 块省 14 分钟；慢盘省更多） | 已改 |
| 连接复用 | ÷1.05–1.15 | 已改 |
| JSON 修复 | ÷2（对该模型） | 已改 |
| 并发 4→16 | ÷4 | 改默认 8，建议你调 16 |
| max_tokens 防跑飞 | 消除 250–650 s 长尾 | 已改 |
| prompt 瘦身 | ÷2.5 | 已改（`promptProfile=compact`） |
| 块大小 1200→3000 | ÷2.6 | 已改（可配） |

只做「已改」部分：144 s/调用 → 约 60 s，并发 8 → **约 7.5 s/块**，600 块从 6 小时降到 **75 分钟**。

再做两个「待决策」项：**约 1.2 s/块**，15 万块 → 从 62 天降到 **约 2 天**。

## 7. 调参速查（`backend/.env`）

```ini
LLM_CONCURRENCY=16      # 并发，最大杠杆；限流会自动减半重试
LLM_MAX_TOKENS=2000     # 单块生成上限，防跑飞
LLM_PROGRESS_EVERY=200  # 每 N 个流事件打一行心跳（别调小，每行一次写库）
LLM_TIMEOUT=90          # 流式下这是"两个字节之间的超时"，不是总时长
```

## 8. 复现方式

```powershell
cd backend
.\.venv\Scripts\python.exe ..\outputs\bench_ingest.py    # 解析/切块/分词基准
.\.venv\Scripts\python.exe ..\outputs\bench_joblog.py    # 任务日志写库基准
.\.venv\Scripts\python.exe ..\outputs\bench_excel.py     # Excel 解析→切块→建索引
.\.venv\Scripts\python.exe manage.py test api            # 89 项测试全绿
```

## 9. 第二轮：Excel 入库 + 参数可配（已落地）

**Excel 全部入库。** `.xlsx`/`.xlsm` 用 openpyxl（已有依赖），`.xls` 用新增的 xlrd。之前 `SUPPORTED_RAW_SUFFIXES` 里根本没有 Excel，`.xlsx` 被 `discover_project_documents` 直接过滤掉——不报错、不提示、不计入总数，所以「200MB 里有 Excel」这件事在日志里完全看不出来。

解析方式：工作表名作为章节标题，首行作为表头，之后每行渲染成 `列名=值；列名=值`。不用 `a | b | c` 位置拼接，因为 `0.01`、`100000` 这类值脱离列名后无法判断含义，抽出来的对象只能是噪音。

**读不了的格式不再静默消失。** `.doc`/`.ppt`/`.wps` 等进入 `UNSUPPORTED_RAW_SUFFIXES`，读取目录时返回 `unsupported` 列表，前端在文件表下方显示黄色提示，摄入日志也打一行「跳过 N 个无法解析的文件」。

**两个参数都有配置入口**（UI「2. 抽取设置」+ API 参数 + 环境变量三层）：

| 参数 | 默认 | UI | API | 环境变量 |
|---|---|---|---|---|
| 抽取详略 | `compact` | 下拉框 | `promptProfile` | `INGEST_PROMPT_PROFILE` |
| 单块长度 | 3000 | 数字框 | `chunkTargetChars` | `INGEST_CHUNK_TARGET_CHARS` |
| 块重叠 | 200 | 数字框 | `chunkOverlapChars` | `INGEST_CHUNK_OVERLAP_CHARS` |

切片参数计入 `parser_version`（`2026-10-23.1-c3000-o200`）。改参数后 `documents.parser_version` 与 meta 不一致，下次构建自动重切；断点 key 含 `contentHash`，旧断点自然不命中，不会把 1200 字符的旧答案塞进 3000 字符的新块。

**顺带修掉一个切块缺陷**：重叠区原来按字符硬切，Excel 第二块会从 `交所；产品=产品39` 这种半行开始。现在重叠起点向前对齐到最近的 `\n\n`，块首一定是完整记录。

实测（`outputs/bench_excel.py`，300 行 6 列工作簿）：解析 → 18 块（1200 字符）/ 7 块（3000 字符）→ 建索引 76 ms，`.xlsx` 后缀正确入库。

**注意**：`.xls` 需要 `xlrd`，已加入 `requirements.txt`。内网部署要么带上 wheel，要么把旧 `.xls` 另存为 `.xlsx`——缺 xlrd 时 `.xls` 会报「缺少 xlrd」，不会静默跳过。
