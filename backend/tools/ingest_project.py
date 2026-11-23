import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
import hashlib
import json
import os
import re
import socket
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path

import requests
from dotenv import load_dotenv


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace", line_buffering=True)

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
load_dotenv(ROOT / ".env", override=False)

LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "90"))
LLM_STREAM = os.getenv("LLM_STREAM", "1").lower() not in {"0", "false", "no", "off"}
LLM_CONCURRENCY = max(1, int(os.getenv("LLM_CONCURRENCY", "4")))
LLM_MAX_TOKENS = max(256, int(os.getenv("INGEST_MAX_TOKENS", "4096")))
INGEST_THINKING = os.getenv("INGEST_THINKING", "auto").strip().lower()
LLM_REQUEST_TIMEOUT = max(1, int(os.getenv("INGEST_REQUEST_TIMEOUT", "90")))
FIRST_CONTENT_TIMEOUT = max(1, int(os.getenv("INGEST_FIRST_CONTENT_TIMEOUT", "45")))
INGEST_RETRIES = max(0, min(3, int(os.getenv("INGEST_RETRIES", "1"))))
PROMPT_PROFILE = os.getenv("INGEST_PROMPT_PROFILE", "compact")
_HTTP_LOCAL = threading.local()
MAX_ITEMS_PER_CHUNK = 10
DEFAULT_MAX_OBJECTS = max(1, int(os.getenv("MAX_KNOWLEDGE_OBJECTS", "160")))
INGESTION_SCHEMA_VERSION = 3
CHECKPOINT_SCHEMA_VERSION = 1
EXTRACTION_PROMPT_VERSION = "claim-v2-canonical-chunks"
from api.ingestion_prompts import load_prompts, render_prompt, prompt_fingerprint
PROMPTS = load_prompts()
_ACTIVE_LOCK = threading.Lock()
_ACTIVE_RESPONSES = {}
_CANCELLED = threading.Event()

EXTRACTION_SCHEMA = {"summary": "事实性摘要", "objects": [{"name": "知识对象名", "aliases": [],
    "summary": "原文说明", "domainType": "other", "ontologyKind": "unspecified", "keywords": []}],
    "relations": [{"source": "对象名", "relation": "明确关系", "target": "对象名", "qualifiers": {}}]}

from api.document_conversion import SUPPORTED_RAW_SUFFIXES as SUPPORTED_SUFFIXES
DOMAIN_TYPES = {
    "organization", "role", "system", "product", "document", "interface",
    "message", "event", "process", "rule", "condition", "metric", "term", "other",
}
ONTOLOGY_KINDS = {"individual", "class", "concept", "unspecified"}


class ModelRateLimitError(RuntimeError):
    def __init__(self, message, retry_after_seconds=None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class ExtractionOutputLimitError(RuntimeError):
    pass


class IncompleteIngestionError(RuntimeError):
    pass


class IngestionCancelled(RuntimeError):
    pass


class FirstContentTimeout(TimeoutError):
    pass


def register_response(response):
    deadline = getattr(_HTTP_LOCAL, "deadline", None)
    if deadline is None:
        return
    with _ACTIVE_LOCK:
        deadline["response"] = response
        _ACTIVE_RESPONSES[deadline["id"]] = response
        expired = deadline["expired"]
    if expired or _CANCELLED.is_set():
        abort_response(response)
        raise IngestionCancelled("请求已中止") if _CANCELLED.is_set() else TimeoutError("请求超过截止时间")


def abort_response(response):
    # Shutdown the socket before close(): close alone can wait for a blocked reader.
    raw = getattr(response, "raw", None)
    connection = getattr(raw, "_connection", None)
    sock = getattr(connection, "sock", None)
    if not isinstance(sock, socket.socket):
        fp = getattr(getattr(raw, "_fp", None), "fp", None)
        sock = getattr(getattr(fp, "raw", None), "_sock", None)
    if isinstance(sock, socket.socket):
        try:
            sock.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass
        try:
            socket.close(sock.detach())
        except OSError:
            pass
        # The owning reader closes the Response in its finally block. Closing
        # the same buffered reader from two threads can block on Windows.
        return
    try:
        response.close()
    except OSError:
        pass


def cancel_active_requests():
    _CANCELLED.set()
    with _ACTIVE_LOCK:
        responses = list(_ACTIVE_RESPONSES.values())
    for response in responses:
        abort_response(response)


def output_limit_error(metrics):
    limit = metrics.get('requestMaxTokens') or LLM_MAX_TOKENS
    if metrics.get('reasoningChars', 0) and not metrics.get('outputChars', 0):
        return ExtractionOutputLimitError(f'模型耗尽 {limit} token 输出预算，仅返回思考内容，没有生成抽取 JSON；请关闭抽取思考模式或提高 INGEST_MAX_TOKENS 后重试。未发布不完整知识。')
    return ExtractionOutputLimitError(f'抽取输出达到 {limit} token 上限；请提高 INGEST_MAX_TOKENS 后重试。未发布不完整知识。')


def perf_event(stage, **fields):
    # Counts and timings only: never write prompts, responses or credentials.
    print("PERF " + json.dumps({"stage": stage, "at": datetime.now().astimezone().isoformat(timespec="seconds"),
                               "emittedAt": time.time(),
                               **fields}, ensure_ascii=False), flush=True)


def main():
    global PROMPT_PROFILE, PROMPTS
    args = parse_args()
    PROMPT_PROFILE = args.prompt_profile
    PROMPTS = load_prompts(args.prompt_file)
    _CANCELLED.clear()
    project_dir = Path(args.project_dir).resolve()
    from api.file_paths import fs_path
    raw_dir = fs_path(Path(args.raw_dir).resolve() if args.raw_dir else project_dir / "raw" / "sources")

    if not raw_dir.exists():
        raise SystemExit(f"原文目录不存在：{raw_dir}")
    if not LLM_API_KEY and not args.allow_heuristic:
        raise SystemExit("LLM_API_KEY 未配置。若只想测试脚本结构，可加 --allow-heuristic。")

    docs = discover_documents(raw_dir, args.limit_docs)
    if args.source_file:
        requested = {value.replace("\\", "/") for value in args.source_file}
        docs = [path for path in discover_documents(raw_dir, 0) if path.relative_to(raw_dir).as_posix() in requested]
        if {path.relative_to(raw_dir).as_posix() for path in docs} != requested:
            raise ValueError("指定文档不存在或不是支持的原文相对路径")
    from api.document_versions import read_catalog
    if not docs and not read_catalog({"path": str(project_dir)}):
        raise SystemExit(f"没有找到可解析文档：{raw_dir}")

    print(f"读取原文目录：{raw_dir}", flush=True)
    print(f"待处理文档数：{len(docs)}", flush=True)

    aggregate = {
        "sources": [],
        "objects": {},
        "relations": [],
    }

    from api.search_index import get_ingestion_documents
    from api.ingestion_staging import prepare_markdown, stage_prepared_import, finish_staging, save_report

    project = {"id": str(project_dir), "path": str(project_dir), "rawDir": str(raw_dir)}
    selected_refs = sorted(requested) if args.source_file else ([path.relative_to(raw_dir).as_posix() for path in docs] if args.limit_docs else None)
    run_id = Path(args.checkpoint_dir).name if args.checkpoint_dir else uuid.uuid4().hex
    artifact_dir = fs_path(project_dir / ".index" / "ingestion-artifacts" / run_id)
    print(f"阶段 1/2：转换 Markdown，中间产物：{artifact_dir}", flush=True)
    items, converted, report = prepare_markdown(project, artifact_dir, selected_refs)
    print(f"Markdown 转换完成：成功 {len(converted)}，失败 {len(items) - len(converted)}；失败清单：{report['manifestPath']}", flush=True)
    if not converted:
        raise IncompleteIngestionError(f"没有转换成功的文档；请查看 {report['manifestPath']}")
    print("阶段 2/2：仅对转换成功的文档构建索引、抽取知识", flush=True)
    index_started = time.perf_counter()
    perf_event("index_start", documents=len(converted), artifactDir=str(artifact_dir))
    stage_path = str(Path(args.ingestion_output).with_suffix(".db")) if args.ingestion_output else str(artifact_dir / "staged.db")
    project, baseline = stage_prepared_import(project, stage_path, items, converted, report)
    save_report(artifact_dir, report)
    source_refs = [row["sourceRef"] for row in report["documents"] if row["status"] == "indexed"]
    prepared_docs = get_ingestion_documents(project, source_refs) if source_refs else []
    total_sections = sum(len(document["sections"]) for document in prepared_docs)
    perf_event("index_ready", seconds=round(time.perf_counter() - index_started, 3),
               documents=len(prepared_docs), chunks=total_sections,
               sourceBytes=sum(path.stat().st_size for path in docs),
               inputChars=sum(len(section["text"]) for doc in prepared_docs for section in doc["sections"]))
    for index, document in enumerate(prepared_docs, start=1):
        print(
            f"[文档 {index}/{len(prepared_docs)}] 使用标准 V7 Chunk "
            f"{document['sourceRef']}（{len(document['sections'])} 块）",
            flush=True,
        )

    if not total_sections:
        finish_staging(project, report, set())
        raise IncompleteIngestionError(f"没有可抽取文档；请查看 {report['manifestPath']}")

    checkpoint_dir = fs_path(Path(args.checkpoint_dir).resolve()) if args.checkpoint_dir else None
    if checkpoint_dir:
        write_checkpoint_manifest(checkpoint_dir, project_dir, raw_dir, total_sections)

    print(f"共 {total_sections} 个标题块，LLM 并发数：{args.llm_concurrency}，抽取模式：{PROMPT_PROFILE}", flush=True)
    perf_event("extraction_settings", batchChars=args.extraction_batch_chars, batchSize=args.extraction_batch_size,
               firstContentTimeout=FIRST_CONTENT_TIMEOUT, requestTimeout=LLM_REQUEST_TIMEOUT, retries=INGEST_RETRIES,
               promptProfile=PROMPT_PROFILE, promptFingerprint=prompt_fingerprint(PROMPTS, PROMPT_PROFILE))
    results = {}
    tasks = {}
    global_index = 0
    reused = 0
    completed = 0
    failures = []
    extraction_started = time.perf_counter()
    last_perf = extraction_started

    def extraction_perf(stage):
        elapsed = time.perf_counter() - extraction_started
        perf_event(stage, seconds=round(elapsed, 3), completed=completed, reused=reused, total=total_sections,
                   failed=len(failures), processed=completed + len(failures),
                   newChunksPerMinute=round((completed - reused) * 60 / max(elapsed, .001), 3),
                   concurrency=args.llm_concurrency, pending=len(tasks))

    def collect_completed():
        nonlocal completed, last_perf
        done, _ = wait(tasks, return_when=FIRST_COMPLETED)
        for future in done:
            doc_index = tasks.pop(future)
            group_result = future.result()
            for section_index, result in group_result["successes"]:
                results[(doc_index, section_index)] = result
                completed += 1
            failures.extend(group_result["failures"])
        print(f"PROGRESS chunks {completed} {total_sections}", flush=True)
        if time.perf_counter() - last_perf >= 10:
            extraction_perf("extraction_progress")
            last_perf = time.perf_counter()

    print(f"PROGRESS chunks 0 {total_sections}", flush=True)
    executor = ThreadPoolExecutor(max_workers=args.llm_concurrency, thread_name_prefix="wiki-llm")
    try:
        for doc_index, document in enumerate(prepared_docs):
            missing = []
            for section_index, section in enumerate(document["sections"]):
                global_index += 1
                label = section_label(global_index, total_sections, document["sourceRef"], section)
                checkpoint_key = section_checkpoint_key(document["sourceRef"], section, args.allow_heuristic)
                cached = load_section_checkpoint(checkpoint_dir, checkpoint_key)
                if cached is not None:
                    results[(doc_index, section_index)] = cached
                    reused += 1
                    completed += 1
                    continue
                missing.append((section_index, section, checkpoint_key, label))
            for group in group_sections(missing, args.extraction_batch_chars, args.extraction_batch_size):
                future = executor.submit(extract_group, checkpoint_dir, document["sourceRef"], group, args.allow_heuristic)
                tasks[future] = doc_index
                if len(tasks) >= args.llm_concurrency * 2:
                    collect_completed()

        print(f"CHECKPOINT reused {reused} missing {total_sections - reused}", flush=True)
        print(f"PROGRESS chunks {completed} {total_sections}", flush=True)
        while tasks:
            collect_completed()
    except Exception:
        extraction_perf("extraction_failed")
        for future in tasks:
            future.cancel()
        cancel_active_requests()
        executor.shutdown(wait=False, cancel_futures=True)
        raise
    else:
        executor.shutdown(wait=True)
        extraction_perf("extraction_end")

    if checkpoint_dir:
        write_atomic_json(checkpoint_dir / "failures.json", {"failures": failures})
    failed_refs = {row["sourceRef"] for row in failures}
    report["chunkFailures"] = failures
    finish_staging(project, report, failed_refs)
    if not report["succeededDocuments"]:
        raise IncompleteIngestionError(f"没有完整抽取成功的文档；成功块已缓存，请查看 {report['manifestPath']}")

    merge_started = time.perf_counter()
    for doc_index, document in enumerate(prepared_docs):
        if document["sourceRef"] in failed_refs:
            continue
        source_summary_parts = []
        for section_index, section in enumerate(document["sections"]):
            extraction = results[(doc_index, section_index)]
            extraction = limit_extraction_items(extraction, args.max_items_per_chunk)
            section["summary"] = str(extraction.get("summary") or "").strip()
            source_summary_parts.append(extraction.get("summary", ""))
            merge_items(aggregate["objects"], extraction.get("objects", []), document["sourceRef"], section["text"], section)
            merge_relations(
                aggregate["relations"], extraction.get("relations", []), document["sourceRef"], section
            )

        path = document["path"]
        aggregate["sources"].append({
            "name": path.name,
            "relativePath": document["sourceRef"],
            "path": path,
            "sha1": file_sha1(path),
            "summary": "\n".join(part for part in source_summary_parts if part).strip(),
            "sectionCount": len(document["sections"]),
            "sections": document["sections"],
        })

    post_process_aggregate(aggregate, args.max_objects)
    output = build_ingestion_payload(aggregate)
    output["ingestionReport"] = report
    if args.ingestion_output:
        if project.get("_indexPath"):
            output["documentImport"] = {"baseline": baseline, "sources": source_refs}
        write_ingestion_payload(Path(args.ingestion_output).resolve(), output)
    else:
        from api.search_index import publish_document_import
        live_project = {key: value for key, value in project.items() if key != "_indexPath"}
        published = publish_document_import(live_project, stage_path, baseline, output)
        if not published.get("ok"):
            raise IncompleteIngestionError("索引发布失败，文件尚未入库")
        from api.ingestion_staging import reconcile_publication
        reconcile_publication(live_project, report)
    perf_event("merge_payload_end", seconds=round(time.perf_counter() - merge_started, 3),
               objects=len(aggregate["objects"]), relations=len(aggregate["relations"]))
    print("完成。", flush=True)


def parse_args():
    parser = argparse.ArgumentParser(description="Extract local knowledge into a V7 ingestion payload.")
    parser.add_argument("--project-dir", required=True, help="本地知识项目目录。")
    parser.add_argument("--raw-dir", help="原文目录，默认是 <project-dir>/raw/sources。")
    parser.add_argument("--limit-docs", type=int, default=0, help="仅处理前 N 个文档，0 表示全部。")
    parser.add_argument("--source-file", action="append", default=[], help="仅重导入指定原文相对路径，可重复传入。")
    parser.add_argument("--llm-concurrency", type=int, default=LLM_CONCURRENCY, help="并行调用 LLM 的标题块数量，默认读取 LLM_CONCURRENCY 或使用 4。")
    parser.add_argument("--max-items-per-chunk", type=int, default=MAX_ITEMS_PER_CHUNK, help="单个切片最多保留的知识对象候选数，默认 10。")
    parser.add_argument("--max-objects", type=int, default=DEFAULT_MAX_OBJECTS, help="全局筛选后最多保留的知识对象总数，默认 160。")
    parser.add_argument("--max-chunks-per-doc", type=int, default=0, help=argparse.SUPPRESS)
    parser.add_argument("--allow-heuristic", action="store_true", help="没有 LLM_API_KEY 时使用简易规则生成，便于测试流程。")
    parser.add_argument("--ingestion-output", help="把结构化抽取结果写入一次性 V7 摄入载荷。")
    parser.add_argument("--checkpoint-dir", help="章节级断点目录；已完成章节会直接复用。")
    parser.add_argument("--prompt-file", help="任务提示词快照文件。")
    parser.add_argument("--extraction-batch-chars", type=int, default=int(os.getenv("INGEST_BATCH_CHARS", "3000")))
    parser.add_argument("--extraction-batch-size", type=int, default=int(os.getenv("INGEST_BATCH_SIZE", "6")))
    parser.add_argument("--prompt-profile", choices=("compact", "standard"), default=PROMPT_PROFILE,
                        help="compact 缩短索引摘要；standard 保留原抽取提示词。")
    args = parser.parse_args()
    if not 500 <= args.extraction_batch_chars <= 12000 or not 1 <= args.extraction_batch_size <= 12:
        parser.error("抽取批次长度应为500–12000字符，块数应为1–12")
    return args


def discover_documents(raw_dir, limit):
    docs = sorted(
        path for path in raw_dir.rglob("*")
        if path.is_file()
        and not any(part in {".index", "document-history"} for part in path.relative_to(raw_dir).parts)
        and not path.name.startswith("~$")
        and path.suffix.lower() in SUPPORTED_SUFFIXES
    )
    return docs[:limit] if limit and limit > 0 else docs


def section_label(index, total, source_name, section):
    heading = heading_path_text(section.get("headingPath")) or source_name
    return f"[块 {index}/{total}] {source_name} · {heading}"


def heading_path_text(value):
    return " > ".join(str(part).strip() for part in value if str(part).strip()) if isinstance(value, list) else str(value or "").strip()


def section_checkpoint_key(source_ref, section, allow_heuristic=False):
    identity = {
        "sourceRef": str(source_ref).replace("\\", "/"),
        "sectionId": section.get("sectionId"),
        "contentHash": section.get("contentHash"),
        "startOffset": section.get("startOffset"),
        "endOffset": section.get("endOffset"),
        "model": LLM_MODEL if LLM_API_KEY else "heuristic",
        "apiBase": LLM_API_BASE if LLM_API_KEY else "",
        "promptVersion": EXTRACTION_PROMPT_VERSION,
        "allowHeuristic": bool(allow_heuristic),
        "promptFingerprint": prompt_fingerprint(PROMPTS, PROMPT_PROFILE),
    }
    if PROMPT_PROFILE != "standard":
        identity["promptProfile"] = PROMPT_PROFILE
    encoded = json.dumps(identity, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def checkpoint_file(checkpoint_dir, checkpoint_key):
    from api.file_paths import fs_path
    return fs_path(Path(checkpoint_dir) / "sections" / f"{checkpoint_key}.json")


def load_section_checkpoint(checkpoint_dir, checkpoint_key):
    if not checkpoint_dir:
        return None
    try:
        payload = json.loads(checkpoint_file(checkpoint_dir, checkpoint_key).read_text(encoding="utf-8"))
        result = payload.get("result")
        if (
            payload.get("schemaVersion") == CHECKPOINT_SCHEMA_VERSION
            and payload.get("checkpointKey") == checkpoint_key
            and isinstance(result, dict)
            and isinstance(result.get("objects", []), list)
            and isinstance(result.get("relations", []), list)
        ):
            return result
    except (OSError, ValueError, TypeError):
        pass
    return None


def write_atomic_json(target, payload):
    from api.file_paths import fs_path
    target = fs_path(target)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(".write-" + uuid.uuid4().hex + ".tmp")
    try:
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


def save_section_checkpoint(checkpoint_dir, checkpoint_key, source_ref, section, result):
    if not checkpoint_dir:
        return
    write_atomic_json(checkpoint_file(checkpoint_dir, checkpoint_key), {
        "schemaVersion": CHECKPOINT_SCHEMA_VERSION,
        "checkpointKey": checkpoint_key,
        "sourceRef": source_ref,
        "sectionId": section.get("sectionId"),
        "headingPath": heading_path_text(section.get("headingPath")),
        "chunkId": section.get("chunkId"),
        "documentVersionId": section.get("documentVersionId"),
        "structuralNodeId": section.get("structuralNodeId"),
        "contentHash": section.get("contentHash", ""),
        "model": LLM_MODEL if LLM_API_KEY else "heuristic",
        "promptVersion": EXTRACTION_PROMPT_VERSION,
        "completedAt": datetime.now().isoformat(timespec="seconds"),
        "result": result,
    })


def write_checkpoint_manifest(checkpoint_dir, project_dir, raw_dir, total_sections):
    write_atomic_json(Path(checkpoint_dir) / "manifest.json", {
        "schemaVersion": CHECKPOINT_SCHEMA_VERSION,
        "projectDir": str(project_dir),
        "rawDir": str(raw_dir),
        "model": LLM_MODEL if LLM_API_KEY else "heuristic",
        "promptVersion": EXTRACTION_PROMPT_VERSION,
        "totalSections": total_sections,
        "updatedAt": datetime.now().isoformat(timespec="seconds"),
    })


def extract_and_checkpoint_section(checkpoint_dir, checkpoint_key, source_name, section, allow_heuristic, status_label):
    result = retry_extraction(lambda: extract_section_with_llm(
        source_name,
        section["text"],
        allow_heuristic,
        heading_path_text(section.get("headingPath")),
        status_label,
    ), status_label)
    save_section_checkpoint(checkpoint_dir, checkpoint_key, source_name, section, result)
    return result


def fatal_extraction_error(exc):
    return isinstance(exc, (ModelRateLimitError, IngestionCancelled)) or (
        isinstance(exc, requests.HTTPError) and exc.response is not None
        and exc.response.status_code in {400, 401, 403, 404})


def retry_extraction(action, label):
    for attempt in range(INGEST_RETRIES + 1):
        if _CANCELLED.is_set():
            raise IngestionCancelled("任务已中止")
        try:
            return action()
        except Exception as exc:
            retryable = isinstance(exc, (TimeoutError, requests.ConnectionError, requests.Timeout)) or (
                isinstance(exc, requests.HTTPError) and exc.response is not None and exc.response.status_code >= 500)
            if not retryable or fatal_extraction_error(exc) or attempt == INGEST_RETRIES:
                raise
            delay = min(8, 2 ** (attempt + 1))
            perf_event("chunk_retry", label=label, attempt=attempt + 1, delaySeconds=delay, errorType=type(exc).__name__)
            if _CANCELLED.wait(delay):
                raise IngestionCancelled("任务已中止")


def group_sections(entries, max_chars, max_size):
    group, chars = [], 0
    for entry in entries:
        size = len(entry[1]["text"]) + len(heading_path_text(entry[1].get("headingPath"))) + 80
        if group and (chars + size > max_chars or len(group) >= max_size):
            yield group
            group, chars = [], 0
        group.append(entry)
        chars += size
    if group:
        yield group


def extract_group(checkpoint_dir, source_name, group, allow_heuristic):
    result = {"successes": [], "failures": []}
    batch_results = None
    if len(group) > 1 and LLM_API_KEY:
        inputs = [{"chunkId": section["chunkId"], "headingPath": heading_path_text(section.get("headingPath")),
                   "text": section["text"]} for _, section, _, _ in group]
        schema = {"results": [{"chunkId": item["chunkId"], **EXTRACTION_SCHEMA} for item in inputs]}
        prompt = render_prompt(PROMPTS, PROMPT_PROFILE, source_name, "批量切片，路径分别见每块",
                               json.dumps(inputs, ensure_ascii=False), json.dumps(schema, ensure_ascii=False))
        try:
            raw = retry_extraction(lambda: chat_json(prompt, status_label=group[0][3] + f" 批量{len(group)}块"), group[0][3])
            rows = raw.get("results")
            ids = [row.get("chunkId") for row in rows] if isinstance(rows, list) and all(isinstance(row, dict) for row in rows) else []
            expected = [item["chunkId"] for item in inputs]
            if len(ids) != len(expected) or set(ids) != set(expected) or len(set(ids)) != len(ids):
                raise ValueError("批量响应切片编号不完整或重复")
            if any(not isinstance(row.get("objects"), list) or not isinstance(row.get("relations"), list) for row in rows):
                raise ValueError("批量响应缺少对象或关系数组")
            batch_results = dict(zip(ids, rows))
        except Exception as exc:
            if fatal_extraction_error(exc):
                raise
            perf_event("batch_fallback", label=group[0][3], chunks=len(group), errorType=type(exc).__name__)
    for section_index, section, key, label in group:
        try:
            if batch_results is None:
                extracted = extract_and_checkpoint_section(checkpoint_dir, key, source_name, section, allow_heuristic, label)
            else:
                extracted = batch_results[section["chunkId"]]
                save_section_checkpoint(checkpoint_dir, key, source_name, section, extracted)
        except Exception as exc:
            if fatal_extraction_error(exc) or isinstance(exc, OSError) and not isinstance(exc, (TimeoutError, requests.RequestException)):
                raise
            failure = {"sourceRef": source_name, "chunkId": section.get("chunkId"), "checkpointKey": key,
                       "label": label, "errorType": type(exc).__name__, "message": str(exc)[:2000]}
            result["failures"].append(failure)
            if checkpoint_dir:
                write_atomic_json(Path(checkpoint_dir) / "failures" / f"{key}.json", failure)
            perf_event("chunk_failed", **failure)
            continue
        result["successes"].append((section_index, extracted))
        if checkpoint_dir:
            (Path(checkpoint_dir) / "failures" / f"{key}.json").unlink(missing_ok=True)
        print(f"{label} 完成并缓存", flush=True)
    return result


def extract_section_with_llm(source_name, section_text, allow_heuristic, heading_path=None, status_label=""):
    if not LLM_API_KEY:
        return heuristic_extract(section_text)

    prompt = render_prompt(PROMPTS, PROMPT_PROFILE, source_name,
                           heading_path_text(heading_path) or "未识别路径", section_text,
                           json.dumps(EXTRACTION_SCHEMA, ensure_ascii=False))
    try:
        return chat_json(prompt, status_label=status_label)
    except (ModelRateLimitError, IngestionCancelled):
        raise
    except Exception as exc:
        if not allow_heuristic:
            raise
        print(f"{status_label or source_name} LLM 抽取失败，使用规则降级：{exc}", flush=True)
        return heuristic_extract(section_text)


def chat_json(prompt, status_label=""):
    payload = {
        "model": LLM_MODEL,
        "messages": [
            {"role": "system", "content": PROMPTS["system"]},
            {"role": "user", "content": prompt},
        ],
        "temperature": 0,
    }
    text = request_chat_text(payload, status_label)
    try:
        return parse_extraction_json(text)
    except json.JSONDecodeError as original_error:
        perf_event("json_repair", label=status_label)
        prefix = status_label or "LLM"
        print(f"{prefix} 返回的 JSON 不合法，正在请求模型修复一次", flush=True)
        repair_payload = {
            "model": LLM_MODEL,
            "messages": [
                {"role": "system", "content": "你是 JSON 修复器。只返回修复后的合法 JSON，不要解释或输出 Markdown。"},
                {"role": "user", "content": f"修复下面的 JSON，保持原字段和值，不增加事实：\n\n{text}"},
            ],
            "temperature": 0,
        }
        repaired = request_chat_text(repair_payload, f"{prefix} JSON修复")
        try:
            return parse_extraction_json(repaired)
        except json.JSONDecodeError as repair_error:
            raise RuntimeError(
                f"LLM JSON 修复失败：首次响应 {original_error.msg}（行 {original_error.lineno} 列 {original_error.colno}，"
                f"不记录响应正文）；修复响应 {repair_error.msg}（行 {repair_error.lineno} 列 {repair_error.colno}）"
            ) from repair_error


def request_chat_text(payload, status_label=""):
    if _CANCELLED.is_set():
        raise IngestionCancelled("任务已中止")
    payload = {"max_tokens": LLM_MAX_TOKENS, **payload}
    mode = INGEST_THINKING
    if mode not in {'auto', 'enabled', 'disabled'}:
        raise ValueError('INGEST_THINKING 必须为 auto、enabled 或 disabled')
    model = str(payload.get('model') or '').casefold().rsplit('/', 1)[-1]
    if mode == 'auto':
        mode = 'disabled' if model.startswith('deepseek-v4') or model == 'deepseek-flash' else None
    if mode and 'thinking' not in payload and 'reasoning_effort' not in payload:
        payload['thinking'] = {'type': mode}
    metrics = {"requestId": uuid.uuid4().hex[:12], "label": status_label,
               "inputChars": sum(len(str(message.get("content") or "")) for message in payload.get("messages", [])),
               "outputChars": 0, "reasoningChars": 0, "events": 0,
               "firstEventSeconds": None, "firstContentSeconds": None, "completionTokens": None,
               "requestModel": payload.get("model"), "requestTemperature": payload.get("temperature"),
               "requestThinking": (payload.get('thinking') or {}).get('type'),
               "requestMaxTokens": payload.get("max_tokens"), "requestStream": bool(LLM_STREAM),
               "proxyEnabled": bool(requests.utils.get_environ_proxies(f"{LLM_API_BASE}/chat/completions"))}
    _HTTP_LOCAL.metrics = metrics
    deadline = {"id": metrics["requestId"], "response": None, "expired": None, "firstContent": threading.Event()}
    _HTTP_LOCAL.deadline = deadline

    def expire(kind):
        with _ACTIVE_LOCK:
            if kind == "first_content" and deadline["firstContent"].is_set():
                return
            if deadline["expired"] is not None:
                return
            deadline["expired"] = kind
            response = deadline["response"]
        if response is not None:
            abort_response(response)

    timers = [threading.Timer(LLM_REQUEST_TIMEOUT, expire, args=("total",))]
    if LLM_STREAM:
        timers.append(threading.Timer(FIRST_CONTENT_TIMEOUT, expire, args=("first_content",)))
    for timer in timers:
        timer.daemon = True
        timer.start()
    started = time.perf_counter()
    success = False
    try:
        if LLM_STREAM:
            text = chat_text_stream({**payload, "stream": True}, status_label=status_label)
        else:
            text = chat_text_blocking(payload, status_label=status_label)
        if deadline["expired"]:
            raise TimeoutError("请求超过截止时间")
        metrics["outputChars"] = len(text)
        success = True
        return text
    except Exception as exc:
        kind = deadline["expired"]
        if _CANCELLED.is_set():
            metrics["errorType"] = "IngestionCancelled"
            raise IngestionCancelled("任务已中止并关闭活动连接") from exc
        metrics["errorType"] = "FirstContentTimeout" if kind == "first_content" else "TimeoutError" if kind else type(exc).__name__
        if kind:
            error = FirstContentTimeout(f"超过 {FIRST_CONTENT_TIMEOUT}s 未收到正文，已关闭请求") if kind == "first_content" else TimeoutError(f"请求超过 {LLM_REQUEST_TIMEOUT}s，已关闭连接")
            raise error from exc
        raise
    finally:
        for timer in timers:
            timer.cancel()
        with _ACTIVE_LOCK:
            _ACTIVE_RESPONSES.pop(deadline["id"], None)
        _HTTP_LOCAL.deadline = None
        seconds = time.perf_counter() - started
        perf_event("llm_request", **metrics, seconds=round(seconds, 3), ok=success,
                   completionTokensPerSecond=(round(metrics["completionTokens"] / max(seconds, .001), 3)
                                              if isinstance(metrics["completionTokens"], (int, float)) else None),
                   outputCharsPerSecond=round(metrics["outputChars"] / max(seconds, .001), 3))


def http_session():
    # A worker owns its Session; requests.Session is not shared across threads.
    if not hasattr(_HTTP_LOCAL, "session"):
        _HTTP_LOCAL.session = requests.Session()
    return _HTTP_LOCAL.session


def chat_text_blocking(payload, status_label=""):
    from api.llm import reasoning_text
    prefix = status_label or "LLM"
    print(f"{prefix} LLM 开始请求", flush=True)
    started_at = time.monotonic()
    response = http_session().post(
        f"{LLM_API_BASE}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
        json=payload,
        timeout=(5, min(LLM_TIMEOUT, LLM_REQUEST_TIMEOUT)),
    )
    try:
        register_response(response)
        raise_for_model_status(response)
        data = response.json()
        metrics = getattr(_HTTP_LOCAL, "metrics", {})
        metrics["completionTokens"] = (data.get("usage") or {}).get("completion_tokens")
        choice = data["choices"][0]
        message = choice.get('message') or {}
        metrics['outputChars'] = len(message.get('content') or '')
        metrics['reasoningChars'] = len(reasoning_text(message))
        metrics['finishReason'] = choice.get('finish_reason')
        if choice.get("finish_reason") == "length":
            raise output_limit_error(metrics)
        text = choice["message"]["content"]
        print(f"{prefix} LLM 完成（{time.monotonic() - started_at:.1f}s，{len(text)} 字符）", flush=True)
        return text
    finally:
        response.close()


def chat_text_stream(payload, status_label=""):
    started_at = time.monotonic()
    print(f"{status_label or 'LLM'} LLM 开始请求", flush=True)
    headers_started = time.perf_counter()
    response = http_session().post(
        f"{LLM_API_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {LLM_API_KEY}",
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
            "Accept-Encoding": "identity",
        },
        json=payload,
        timeout=(5, min(LLM_TIMEOUT, LLM_REQUEST_TIMEOUT, FIRST_CONTENT_TIMEOUT)),
        stream=True,
    )
    try:
        register_response(response)
        metrics = getattr(_HTTP_LOCAL, "metrics", {})
        metrics["headersSeconds"] = round(time.perf_counter() - headers_started, 3)
        metrics["httpStatus"] = response.status_code
        for field, header in (("contentType", "Content-Type"), ("transferEncoding", "Transfer-Encoding"),
                              ("contentEncoding", "Content-Encoding"), ("contentLength", "Content-Length")):
            value = response.headers.get(header)
            metrics[field] = value[:120] if isinstance(value, str) else None
        raise_for_model_status(response)
        return read_chat_stream(response, status_label, started_at)
    finally:
        response.close()


def stream_event_shape(event):
    """Bounded field names/types only; never include model text or error messages."""
    def keys(value):
        return sorted(str(key)[:64] for key in value)[:12] if isinstance(value, dict) else []

    shape = {"eventType": type(event).__name__, "keys": keys(event)}
    if not isinstance(event, dict):
        return shape
    for name in ("data", "output", "message", "error"):
        if name in event:
            shape[name + "Type"] = type(event[name]).__name__
            shape[name + "Keys"] = keys(event[name])
    choices = event.get("choices")
    shape["choicesType"] = type(choices).__name__
    if isinstance(choices, list) and choices and isinstance(choices[0], dict):
        choice = choices[0]
        shape["choiceKeys"] = keys(choice)
        for name in ("delta", "message"):
            if name in choice:
                value = choice[name]
                shape[name + "Type"] = type(value).__name__
                shape[name + "Keys"] = keys(value)
                if isinstance(value, dict):
                    shape[name + "ContentType"] = type(value.get("content")).__name__
                    for field in ("reasoning", "reasoning_content"):
                        if field in value:
                            shape[name + field[0].upper() + field[1:] + "Type"] = type(value[field]).__name__
    return shape


def read_chat_stream(response, status_label, started_at):
    from api.llm import iter_sse_lines, reasoning_text

    chunks = []
    event_count = 0
    last_progress = started_at
    metrics = getattr(_HTTP_LOCAL, "metrics", {})
    prefix = status_label or "LLM"
    print(f"{prefix} LLM 开始接收", flush=True)
    for raw_line in iter_sse_lines(response):
        now = time.monotonic()
        if now - started_at > LLM_REQUEST_TIMEOUT:
            raise TimeoutError(f"{prefix} 抽取超过 {LLM_REQUEST_TIMEOUT}s，请检查模型排队或思考耗时")
        if not raw_line:
            continue
        line = raw_line.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
        if line == "[DONE]":
            break
        if not line:
            continue

        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue

        if metrics.get("firstEventSeconds") is None:
            metrics["firstEventSeconds"] = round(now - started_at, 3)
        metrics["events"] = metrics.get("events", 0) + 1
        shape = stream_event_shape(event)
        shapes = metrics.setdefault("eventShapes", [])
        if shape not in shapes and len(shapes) < 4:
            shapes.append(shape)
            perf_event("llm_event_shape", requestId=metrics.get("requestId"), label=status_label, shape=shape)
        if not isinstance(event, dict):
            raise RuntimeError("模型流式事件必须为 JSON 对象，请核对接口格式")
        if event.get("error") is not None or event.get("type") == "error":
            metrics["serverErrorEvents"] = metrics.get("serverErrorEvents", 0) + 1
            raise RuntimeError("模型流式接口返回 error 事件，请检查模型服务端日志")
        if not event.get("choices"):
            metrics["emptyChoiceEvents"] = metrics.get("emptyChoiceEvents", 0) + 1
        if event.get("usage"):
            metrics["completionTokens"] = event["usage"].get("completion_tokens")
        for choice in event.get("choices", []):
            delta = choice.get("delta") or {}
            reasoning = reasoning_text(delta)
            metrics["reasoningChars"] = metrics.get("reasoningChars", 0) + len(reasoning)
            content = delta.get("content")
            if content is None:
                content = (choice.get("message") or {}).get("content")
            if content:
                deadline = getattr(_HTTP_LOCAL, "deadline", None)
                if deadline is not None:
                    deadline["firstContent"].set()
                if metrics.get("firstContentSeconds") is None:
                    metrics["firstContentSeconds"] = round(now - started_at, 3)
                metrics["outputChars"] = metrics.get("outputChars", 0) + len(content)
                chunks.append(content)
                event_count += 1
            elif not reasoning:
                metrics["emptyDeltaEvents"] = metrics.get("emptyDeltaEvents", 0) + 1
            if choice.get("finish_reason") is not None:
                metrics["finishReason"] = choice["finish_reason"]
            if choice.get("finish_reason") == "length":
                raise output_limit_error(metrics)
        if now - last_progress >= 10:
            print(f"{prefix} LLM 正在接收（{now - started_at:.1f}s，{event_count} 段正文）", flush=True)
            perf_event("llm_wait", **metrics, seconds=round(now - started_at, 3))
            last_progress = now

    text = "".join(chunks)
    if not text.strip():
        raise RuntimeError("LLM 流式返回为空")
    print(f"{prefix} LLM 完成（{time.monotonic() - started_at:.1f}s，{len(text)} 字符）", flush=True)
    return text


def parse_extraction_json(text):
    cleaned = clean_json_text(text)
    try:
        result = json.loads(cleaned)
    except json.JSONDecodeError:
        # Only remove trailing commas outside strings. Never invent truncated facts.
        cleaned = re.sub(r'"(?:\\.|[^"\\])*"|(,)(?=\s*[}\]])',
                         lambda match: "" if match.group(1) else match.group(0), cleaned)
        result = json.loads(cleaned)
    if not isinstance(result, dict) or not isinstance(result.get("objects", []), list) or not isinstance(result.get("relations", []), list):
        raise ValueError("抽取结果必须是包含 objects / relations 数组的 JSON 对象")
    return result


def raise_for_model_status(response):
    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After")
        try:
            retry_after_seconds = max(0, int(retry_after)) if retry_after is not None else None
        except (TypeError, ValueError):
            retry_after_seconds = None
        detail = (response.text or "").strip().replace("\r", " ").replace("\n", " ")[:500]
        message = "模型接口请求过于频繁，生成任务已中断"
        if detail:
            message = f"{message}：{detail}"
        raise ModelRateLimitError(message, retry_after_seconds)
    response.raise_for_status()


def clean_json_text(text):
    text = str(text or "").strip()
    fenced = re.search(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        text = text[start:end + 1]
    return text


def json_error_context(text, error, radius=80):
    text = str(text or "").replace("\r", " ").replace("\n", " ")
    return text[max(0, error.pos - radius):error.pos + radius]


def heuristic_extract(text):
    candidates = re.findall(r"[\u4e00-\u9fffA-Za-z0-9（）()《》]{3,32}", text)
    names = []
    seen = set()
    for candidate in candidates:
        clean = normalize_title(candidate)
        if clean and clean.casefold() not in seen:
            seen.add(clean.casefold())
            names.append(clean)
        if len(names) >= 8:
            break
    return {
        "summary": text[:300],
        "objects": [
            {"name": name, "summary": "", "aliases": [], "keywords": [], "domainType": "other", "ontologyKind": "unspecified"}
            for name in names[:6]
        ],
        "relations": [],
    }


def limit_extraction_items(extraction, limit=MAX_ITEMS_PER_CHUNK):
    """Deterministically enforce the knowledge-object budget for one chunk."""
    extraction = dict(extraction or {})
    candidates = []
    for position, item in enumerate(extraction.get("objects") or []):
        if not isinstance(item, dict) or not normalize_title(item.get("name", "")):
            continue
        summary = str(item.get("summary") or "").strip()
        quality = (
            1 if len(summary) >= 24 else 0,
            1 if item.get("keywords") else 0,
            1 if item.get("aliases") else 0,
            len(summary),
            -position,
        )
        candidates.append((quality, item))

    budget = max(1, min(int(limit or MAX_ITEMS_PER_CHUNK), MAX_ITEMS_PER_CHUNK))
    selected = sorted(candidates, key=lambda value: value[0], reverse=True)[:budget]
    extraction["objects"] = [item for _, item in selected]

    selected_names = {
        canonical_key(item.get("name", ""))
        for _, item in selected
        if canonical_key(item.get("name", ""))
    }
    extraction["relations"] = [
        relation for relation in extraction.get("relations") or []
        if isinstance(relation, dict)
        and canonical_key(relation.get("source", "")) in selected_names
        and canonical_key(relation.get("target", "")) in selected_names
    ]
    return extraction


def merge_items(target, items, source_name, section_text, section=None):
    for item in items or []:
        if not isinstance(item, dict):
            continue
        name = normalize_title(item.get("name", ""))
        if not name:
            continue
        key = canonical_key(name)
        entry = target.setdefault(
            key,
            {
                "name": name,
                "aliases": set(),
                "summaries": [],
                "keywords": set(),
                "sources": set(),
                "evidence": [],
                "mentions": [],
                "versionProfiles": [],
                "domainTypes": set(),
                "ontologyKinds": set(),
                "occurrences": 0,
            },
        )
        entry["aliases"].update(normalize_title(value) for value in item.get("aliases", []) if normalize_title(value))
        entry["keywords"].update(normalize_title(value) for value in item.get("keywords", []) if normalize_title(value))
        entry["domainTypes"].add(normalize_domain_type(item.get("domainType")))
        entry["ontologyKinds"].add(normalize_ontology_kind(item.get("ontologyKind")))
        if item.get("summary"):
            entry["summaries"].append(str(item["summary"]).strip())
        entry["sources"].add(source_name)
        entry["occurrences"] += 1
        excerpt = find_excerpt(section_text, [name, *entry["aliases"], *entry["keywords"]])
        if excerpt and len(entry["evidence"]) < 8:
            entry["evidence"].append({"source": source_name, "excerpt": excerpt})
        if section and section.get("sectionId"):
            entry.setdefault("versionProfiles", []).append({
                "chunkId": section.get("chunkId"),
                "description": str(item.get("summary") or "").strip(),
                "terms": dedupe([name, *(item.get("aliases") or []), *(item.get("keywords") or [])]),
            })
            entry["mentions"].append({
                "sectionId": section["sectionId"],
                "structuralNodeId": section.get("structuralNodeId"),
                "chunkId": section.get("chunkId"),
                "documentVersionId": section.get("documentVersionId"),
                "sourceRelativePath": source_name,
                "sourceChunkNo": section.get("chunkNo", 0),
                "headingPath": heading_path_text(section.get("headingPath")),
                "startOffset": section.get("startOffset", 0),
                "endOffset": section.get("endOffset", 0),
                "evidence": excerpt,
                "summary": str(item.get("summary") or "").strip(),
            })


def merge_relations(target, relations, source_name, section=None):
    for relation in relations or []:
        if not isinstance(relation, dict):
            continue
        source = normalize_title(relation.get("source", ""))
        relation_type = normalize_title(relation.get("relation") or relation.get("type") or "")
        destination = normalize_title(relation.get("target", ""))
        qualifiers = normalize_qualifiers(relation.get("qualifiers") or relation.get("conditions"))
        if source and relation_type and destination and canonical_key(source) != canonical_key(destination):
            target.append({
                "source": source,
                "type": relation_type,
                "target": destination,
                "sourceDocument": source_name,
                "qualifiers": qualifiers,
                "evidence": {
                    "chunkId": section.get("chunkId"),
                    "documentVersionId": section.get("documentVersionId"),
                    "structuralNodeId": section.get("structuralNodeId"),
                    "startOffset": section.get("startOffset", 0),
                    "endOffset": section.get("endOffset", 0),
                } if section else {},
            })


def post_process_aggregate(aggregate, max_objects=DEFAULT_MAX_OBJECTS):
    filter_global_items(aggregate, max_objects)
    resolve_explicit_relations(aggregate)


def item_completeness(entry):
    summaries = [value for value in entry.get("summaries", []) if str(value).strip()]
    longest_summary = max((len(value) for value in summaries), default=0)
    return sum((
        bool(summaries),
        longest_summary >= 40,
        bool(entry.get("evidence")),
        bool(entry.get("aliases") or entry.get("keywords")),
    ))


def item_global_score(entry):
    source_coverage = len(entry.get("sources", []))
    occurrences = int(entry.get("occurrences") or 0)
    return source_coverage * 6 + min(occurrences, 8) * 3 + item_completeness(entry) * 2


def filter_global_items(aggregate, max_objects=DEFAULT_MAX_OBJECTS):
    candidates = []
    for key, entry in aggregate["objects"].items():
        if int(entry.get("occurrences") or 0) < 2 and item_completeness(entry) < 3:
            continue
        candidates.append((item_global_score(entry), len(entry["name"]), key))

    limit = max(1, int(max_objects or DEFAULT_MAX_OBJECTS))
    selected = {key for _, _, key in sorted(candidates, reverse=True)[:limit]}
    aggregate["objects"] = {key: entry for key, entry in aggregate["objects"].items() if key in selected}


def resolve_explicit_relations(aggregate):
    entries = []
    alias_to_key = {}
    for entry in aggregate["objects"].values():
        object_key = canonical_key(entry["name"])
        entry["relations"] = []
        entries.append((entry, object_key))
        for value in [entry["name"], *entry["aliases"]]:
            alias_to_key[canonical_key(value)] = object_key

    key_to_entry = {object_key: entry for entry, object_key in entries}
    resolved = {}
    for relation in aggregate.get("relations", []):
        source_key = alias_to_key.get(canonical_key(relation.get("source", "")))
        target_key = alias_to_key.get(canonical_key(relation.get("target", "")))
        relation_type = normalize_title(relation.get("type", ""))
        if not source_key or not target_key or source_key == target_key or not relation_type:
            continue
        qualifiers = normalize_qualifiers(relation.get("qualifiers"))
        key = (source_key, target_key, canonical_key(relation_type), json.dumps(qualifiers, ensure_ascii=False, sort_keys=True))
        item = resolved.get(key)
        if item is None:
            item = {
                "target": key_to_entry[target_key]["name"],
                "type": relation_type,
                "qualifiers": qualifiers,
                "sourceDocument": [],
                "evidence": [],
            }
            resolved[key] = item
            key_to_entry[source_key]["relations"].append(item)
        source_document = str(relation.get("sourceDocument") or "").strip()
        if source_document and source_document not in item["sourceDocument"]:
            item["sourceDocument"].append(source_document)
        evidence = relation.get("evidence") or {}
        if evidence.get("chunkId") and evidence.get("chunkId") not in {
            value.get("chunkId") for value in item["evidence"]
        }:
            item["evidence"].append(evidence)


def normalize_qualifiers(value):
    if not isinstance(value, dict):
        return {}
    allowed = ("when", "scope", "effectiveFrom", "effectiveTo", "exception", "note")
    return {
        key: str(value[key]).strip()
        for key in allowed
        if value.get(key) is not None and str(value[key]).strip()
    }


def find_excerpt(text, terms):
    folded = text.casefold()
    for term in terms:
        if not term:
            continue
        index = folded.find(str(term).casefold())
        if index == -1:
            continue
        start = max(0, index - 180)
        end = min(len(text), index + len(str(term)) + 260)
        return text[start:end].strip()
    return text[:420].strip()


def structure_object_id(name):
    identity = canonical_key(name)
    return "obj_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def build_ingestion_payload(aggregate):
    sources = []
    for source in aggregate.get("sources", []):
        sections = []
        for section in source.get("sections", []):
            sections.append({
                "sectionId": section.get("sectionId"),
                "structuralNodeId": section.get("structuralNodeId"),
                "chunkId": section.get("chunkId"),
                "documentVersionId": section.get("documentVersionId"),
                "chunkNo": section.get("chunkNo", 0),
                "headingPath": heading_path_text(section.get("headingPath")),
                "contentHash": section.get("contentHash", ""),
                "charCount": section.get("charCount", len(section.get("text") or "")),
                "summary": section.get("summary", ""),
            })
        sources.append({
            "relativePath": source.get("relativePath") or source.get("name"),
            "sha1": source.get("sha1", ""),
            "sections": sections,
        })

    objects = []
    for entry in aggregate.get("objects", {}).values():
        objects.append({
                "objectId": structure_object_id(entry["name"]),
                "name": entry["name"],
                "canonicalName": canonical_key(entry["name"]),
                "aliases": sorted(entry.get("aliases") or []),
                "keywords": sorted(entry.get("keywords") or []),
                "sources": sorted(entry.get("sources") or []),
                "description": "\n".join(dedupe(entry.get("summaries") or []))[:4000],
                "domainType": preferred_type(entry.get("domainTypes"), "other"),
                "ontologyKind": preferred_type(entry.get("ontologyKinds"), "unspecified"),
                "mentions": dedupe_mentions(entry.get("mentions", [])),
                "versionProfiles": entry.get("versionProfiles", []),
                "relations": entry.get("relations", []),
        })
    return {
        "schemaVersion": INGESTION_SCHEMA_VERSION,
        "generatedAt": datetime.now().isoformat(timespec="seconds"),
        "sources": sources,
        "objects": sorted(objects, key=lambda item: item["name"]),
    }


def write_ingestion_payload(target, payload):
    write_atomic_json(target, payload)


def dedupe_mentions(mentions):
    seen = set()
    result = []
    for mention in mentions or []:
        key = (mention.get("sectionId"), canonical_key(mention.get("evidence", "")))
        if not key[0] or key in seen:
            continue
        seen.add(key)
        result.append(dict(mention))
    return result


def dedupe(values):
    seen = set()
    unique = []
    for value in values:
        clean = str(value or "").strip()
        key = clean.casefold()
        if clean and key not in seen:
            seen.add(key)
            unique.append(clean)
    return unique


def normalize_title(value):
    return re.sub(r"\s+", " ", str(value or "").strip(" \t\r\n:：,，.。;；"))


def normalize_domain_type(value):
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in DOMAIN_TYPES else "other"


def normalize_ontology_kind(value):
    normalized = str(value or "").strip().casefold()
    return normalized if normalized in ONTOLOGY_KINDS else "unspecified"


def preferred_type(values, fallback):
    candidates = sorted({str(value).strip() for value in values or [] if str(value).strip()})
    specific = [value for value in candidates if value not in {"other", "unspecified"}]
    return (specific or candidates or [fallback])[0]


def canonical_key(value):
    text = normalize_title(value)
    text = re.sub(r"[《》\"'“”‘’\s_\-—/\\（）()]+", "", text)
    return text.casefold()


def file_sha1(path):
    digest = hashlib.sha1()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    try:
        main()
    except ModelRateLimitError as exc:
        print("TASK_ERROR rate_limit " + json.dumps({
            "message": str(exc),
            "retryAfterSeconds": exc.retry_after_seconds,
        }, ensure_ascii=False), flush=True)
        os._exit(75)
    except ExtractionOutputLimitError as exc:
        print('TASK_ERROR output_limit ' + json.dumps({'message': str(exc)}, ensure_ascii=False), flush=True)
        sys.exit(2)
    except IncompleteIngestionError as exc:
        print('TASK_ERROR chunks_failed ' + json.dumps({'message': str(exc)}, ensure_ascii=False), flush=True)
        sys.exit(2)
    except KeyboardInterrupt:
        cancel_active_requests()
        print("已取消。", flush=True)
        os._exit(130)
    except Exception as exc:
        cancel_active_requests()
        print('TASK_ERROR fatal ' + json.dumps({'message': str(exc)[:2000], 'errorType': type(exc).__name__}, ensure_ascii=False), flush=True)
        # Successful checkpoints are atomically replaced; no partial index is published.
        # Do not let executor's interpreter-exit join wait for pre-header HTTP calls.
        os._exit(1)
