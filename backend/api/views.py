import json
import logging
import subprocess
import sys
import os
import re
import shutil
import threading
import time
import uuid
from functools import wraps
from contextlib import contextmanager
from pathlib import Path

import requests
from django.contrib.auth import authenticate, login as auth_login, logout as auth_logout
from django.http import JsonResponse, StreamingHttpResponse
from django.db.models.deletion import ProtectedError
from django.views.decorators.csrf import csrf_exempt
from django.views.decorators.http import require_http_methods

from .llm import (
    answer_with_llm,
    analyze_query_intent,
    expand_query_with_terms,
    rerank_search_results_with_llm,
    stream_answer_with_llm,
    test_llm_model,
)
from .llm_models import create_llm_model, delete_llm_model, get_llm_model, list_llm_models, update_llm_model
from .models import IngestionJob, Project, EvaluationRun
from .exports import export_project_markdown
from .domain_knowledge import (
    expand_query_with_synonyms,
    dump_synonyms,
    load_synonyms,
    merge_synonyms,
    parse_synonym_text,
    save_synonyms,
)
from .projects import (
    get_project,
    list_projects,
    project_file_content,
    project_files,
    project_graph,
    project_query_terms,
    project_search,
    project_root,
    register_project_directory,
    unregister_project,
    register_ingested_project,
)
from .services import content_text, normalize_search_results, result_path
from .ingestion_files import scan_ingestion_sources, receive_upload, validate_source_assignments, upload_target
from .file_paths import fs_path
from .document_versions import list_editions, update_edition, scope_project, edition_metadata
from .search_index import (
    build_project_index,
    get_indexed_document,
    get_indexed_section_evidence,
    inspect_project_index,
    verify_project_index,
    project_raw_dir,
)
from .targeted_evidence import (
    build_terms,
)


BUILD_JOBS_LOCK = threading.Lock()
ACTIVE_INGESTION_TARGETS = {}
SEARCH_CONTEXT_TTL_SECONDS = 900


def json_body(request):
    if not request.body:
        return {}
    return json.loads(request.body.decode("utf-8"))


def search_context_owner(request):
    if request.user.is_authenticated:
        return f"user:{request.user.pk}"
    if not request.session.session_key:
        request.session.create()
    return f"session:{request.session.session_key}"


def store_search_context(request, project_id, query, query_enrichment, results, version_scope=None):
    now = time.time()
    context_id = uuid.uuid4().hex
    context = {
        "id": context_id,
        "owner": search_context_owner(request),
        "projectId": project_id,
        "query": query,
        "versionScope": version_scope or {},
        "editionIds": sorted({item["editionId"] for item in results if item.get("editionId")}),
        "queryIntent": query_enrichment.get("queryIntent") or {},
        "searchQuery": query_enrichment.get("searchQuery") or query,
        "synonymExpansion": query_enrichment.get("synonymExpansion") or {},
        "chunkIds": [item.get("chunkId") for item in results if isinstance(item, dict) and item.get("chunkId")],
        "createdAt": now,
        "expiresAt": now + SEARCH_CONTEXT_TTL_SECONDS,
    }
    contexts = request.session.get("wiki_search_contexts", {})
    contexts = {
        key: value for key, value in contexts.items()
        if value.get("expiresAt", 0) > now
    }
    contexts[context_id] = context
    if len(contexts) > 5:
        newest = sorted(contexts.values(), key=lambda value: value.get("createdAt", 0), reverse=True)[:5]
        contexts = {value["id"]: value for value in newest}
    request.session["wiki_search_contexts"] = contexts
    request.session.modified = True
    return context_id


def apply_search_context(request, project_id, payload):
    context_id = str(payload.get("searchContextId") or "").strip()
    if not context_id:
        return payload
    context = request.session.get("wiki_search_contexts", {}).get(context_id)
    if not context or context.get("expiresAt", 0) <= time.time():
        return payload
    if context.get("projectId") != project_id or context.get("owner") != search_context_owner(request):
        return payload
    if str(payload.get("query") or "").strip() != context.get("query"):
        return payload
    if (payload.get("versionScope") or {}) != context.get("versionScope", {}):
        raise ValueError("版本范围已变化，请重新检索")
    project = get_project(project_id) if context.get("editionIds") else None
    if project and context.get("editionIds"):
        from .search_index import connect_index
        connection = connect_index(scope_project(project, payload.get("versionScope")))
        try:
            allowed = {edition_metadata(connection, row[0]).get("editionId") for row in connection.execute("SELECT id FROM documents")}
            if not set(context["editionIds"]).issubset(allowed):
                raise ValueError("默认版本已变化，请重新检索")
        finally:
            connection.close()
    return {
        **payload,
        "queryIntent": context.get("queryIntent"),
        "searchQuery": context.get("searchQuery"),
        "synonymExpansion": context.get("synonymExpansion"),
        "chunkIds": payload.get("chunkIds") or context.get("chunkIds") or [],
        "searchContextReused": True,
    }


def public_query_plan(query_enrichment, results):
    exact_objects = []
    exact_phrases = []
    for item in results:
        if not isinstance(item, dict):
            continue
        target = exact_objects if item.get("matchKind") == "object-source-structured" else exact_phrases
        if item.get("matchKind") in {"object-source-structured", "exact-title", "exact-phrase"}:
            target.extend(item.get("matchedTerms") or [])
    return {
        "intent": (query_enrichment.get("queryIntent") or {}).get("intent", "general"),
        "searchQuery": query_enrichment.get("searchQuery") or "",
        "exactObjects": dedupe_terms(exact_objects),
        "exactPhrases": dedupe_terms(exact_phrases),
        "fallbackTerms": query_enrichment.get("expandedTerms") or [],
        "strategy": "structured-first",
    }


def ok(payload, status=200):
    return JsonResponse(payload, status=status, json_dumps_params={"ensure_ascii": False})


def handle_errors(func):
    @wraps(func)
    def wrapper(request, *args, **kwargs):
        try:
            return func(request, *args, **kwargs)
        except requests.HTTPError as exc:
            message = exc.response.text if exc.response is not None else str(exc)
            return ok({"error": "LLM 请求失败", "detail": message}, status=502)
        except json.JSONDecodeError:
            return ok({"error": "请求体不是合法 JSON"}, status=400)
        except ValueError as exc:
            return ok({"error": str(exc)}, status=400)
        except Exception as exc:
            logging.getLogger(__name__).error("API failed: %s (%s)", func.__name__, type(exc).__name__)
            return ok({"error": str(exc)}, status=500)

    return wrapper


def admin_required(view):
    @wraps(view)
    def wrapper(request, *args, **kwargs):
        user = request.user
        if not user.is_authenticated or not user.is_staff:
            return JsonResponse(
                {"error": "需要管理员权限", "needLogin": True, "needAdmin": True},
                status=403,
                json_dumps_params={"ensure_ascii": False},
            )
        return view(request, *args, **kwargs)

    return wrapper


def auth_payload(user):
    return {
        "authenticated": True,
        "username": user.username,
        "isAdmin": bool(user.is_staff),
        "isSuperuser": bool(user.is_superuser),
        "userId": user.id,
    }


@require_http_methods(["GET"])
def health(request):
    return ok({"portal": "ok"})


@csrf_exempt
@require_http_methods(["GET", "PATCH", "DELETE"])
@handle_errors
def projects(request):
    if request.method in {"DELETE", "PATCH"}:
        user = request.user
        if not user.is_authenticated or not user.is_staff:
            return ok({"error": "需要管理员权限", "needLogin": True, "needAdmin": True}, status=403)
        payload = json_body(request)
        project_id = payload.get("id") or payload.get("projectId")
        if not project_id:
            return ok({"error": "缺少 id"}, status=400)
        if request.method == "PATCH":
            name = payload.get("name")
            if not isinstance(name, str) or not name.strip() or len(name.strip()) > 200:
                raise ValueError("显示名称须为1–200字符")
            if not Project.objects.filter(pk=uuid.UUID(str(project_id))).update(name=name.strip()):
                return ok({"error": "项目不存在"}, status=404)
            return ok({"projects": list_projects()})
        if IngestionJob.objects.filter(project_id=project_id, status="running").exists():
            return ok({"error": "项目正在摄入，请等待完成后再移除"}, status=409)
        if EvaluationRun.objects.filter(project_id=project_id, status__in=["queued", "running"]).exists():
            return ok({"error": "项目还有运行中的评测，请先取消或等待完成"}, status=409)
        try:
            return ok({"projects": unregister_project(project_id)})
        except ProtectedError:
            return ok({"error": "项目还有标准问答，请先迁移或删除问答后再移除项目"}, status=409)
    return ok({"projects": list_projects()})


@csrf_exempt
@require_http_methods(["GET", "POST", "PUT", "DELETE"])
@handle_errors
def llm_models(request):
    if request.method == "GET":
        return ok({"models": list_llm_models()})

    user = request.user
    if not user.is_authenticated or not user.is_staff:
        return ok({"error": "需要管理员权限", "needLogin": True, "needAdmin": True}, status=403)

    payload = json_body(request)
    try:
        if request.method == "POST":
            return ok({"model": create_llm_model(payload), "models": list_llm_models()})
        if request.method == "PUT":
            return ok({"model": update_llm_model(payload), "models": list_llm_models()})

        model_id = payload.get("id") or payload.get("modelId")
        if not model_id:
            return ok({"error": "缺少 id"}, status=400)
        return ok({"models": delete_llm_model(model_id)})
    except ValueError as exc:
        return ok({"error": str(exc)}, status=400)


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
def test_llm_model_view(request):
    payload = json_body(request)
    model_config = get_llm_model(payload.get("modelId"))
    return ok(test_llm_model(model_config))


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
@admin_required
def build_ingestion(request, project_id=""):
    payload = json_body(request)
    project_dir = (payload.get("projectDir") or "").strip()
    if not project_dir and project_id:
        project = get_project(project_id)
        project_dir = str((project or {}).get("path") or "").strip()
    if not project_dir:
        return ok({"error": "缺少项目目录，请填写 projectDir"}, status=400)

    project_path = Path(project_dir).resolve()
    if not project_path.exists():
        return ok({"error": f"项目目录不存在：{project_path}"}, status=400)

    try:
        return ok(start_ingestion_job(payload, project_path))
    except IngestionConflict as exc:
        return ok({"error": str(exc), "conflictingJobId": exc.job_id}, status=409)


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
@admin_required
def ingestion_sources(request):
    payload = json_body(request)
    selected = get_project(payload.get("projectId")) if payload.get("projectId") else None
    if payload.get("mode") == "server" and payload.get("sourceDir"):
        raw = Path(payload['sourceDir']).resolve()
        if not fs_path(raw).is_dir():
            raise ValueError(f'服务端本地目录不存在或当前服务无法访问：{raw}')
        if selected:
            project = upload_target(selected)
        else:
            from django.conf import settings
            root = (Path(settings.BASE_DIR) / 'data' / 'upload-projects' / uuid.uuid4().hex).resolve()
            project = {'path': str(root), 'name': payload.get('projectName') or raw.name,
                       'exportDir': str(root / 'exports' / 'wiki')}
        project['rawDir'] = str(raw)
        if project.get('originalProjectPath'):
            project['storageNotice'] = '项目旧位置不可用，将在当前服务器保存索引；文档直接从填写的服务端目录读取。'
        result = scan_ingestion_sources(project)
        Path(project['path']).mkdir(parents=True, exist_ok=True)
        return ok({**result, 'target': project})
    if payload.get("mode") == "server" or not selected:
        directory = payload.get("projectDir")
        if not isinstance(directory, str) or not directory.strip():
            raise ValueError("请填写当前服务器可访问的本地目录")
        root = Path(directory).resolve()
        if not root.is_dir():
            raise ValueError(f"服务端本地目录不存在或当前服务无法访问：{root}")
        project = {"path": str(root), "name": payload.get("projectName") or (selected or {}).get("name") or root.name}
        if payload.get("rawDir"):
            project["rawDir"] = payload["rawDir"]
        elif payload.get("mode") == "server":
            project["rawDir"] = str(root)
    else:
        project = selected
    return ok({**scan_ingestion_sources(project), "target": project})


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
@admin_required
def upload_ingestion(request):
    if request.POST.get("projectId"):
        project = get_project(request.POST["projectId"])
        if not project:
            return ok({"error": "项目不存在"}, status=404)
        project = upload_target(project)
    else:
        name = request.POST.get("projectName", "").strip()
        if not name or len(name) > 200:
            raise ValueError("请填写新项目名称（最多200字符）")
        from django.conf import settings
        base = (Path(settings.BASE_DIR) / "data" / "upload-projects").resolve()
        if request.POST.get("projectDir"):
            root = Path(request.POST["projectDir"]).resolve()
            if not root.is_relative_to(base) or root == base or not root.is_dir():
                raise ValueError("待摄入项目目录无效，请重新添加文件")
        else:
            session_id = request.POST.get('uploadSessionId')
            session_key = f'{request.user.id}-{uuid.UUID(session_id).hex}' if session_id else uuid.uuid4().hex
            root = base / session_key
        project = {"path": str(root), "name": name, "rawDir": str(root / "raw" / "sources"), "exportDir": str(root / "exports" / "wiki")}
    paths = json.loads(request.POST.get("paths", "[]"))
    if not isinstance(paths, list):
        raise ValueError("上传路径须为列表")
    try:
        result = receive_upload(project, request.FILES.getlist("files"), paths)
    except OSError as exc:
        raise ValueError(f"当前服务器无法保存上传文件：{exc.strerror or str(exc)}。请检查服务端存储权限和路径。") from exc
    return ok({**result, "target": project})


class IngestionConflict(RuntimeError):
    def __init__(self, message, job_id):
        super().__init__(message)
        self.job_id = job_id


def start_ingestion_job(payload, project_path, retry_of=None, attempt=1, checkpoint_id=None):
    project_path = Path(project_path).resolve()
    if not project_path.exists():
        raise ValueError(f"项目目录不存在：{project_path}")

    target_project = get_project(payload.get("targetProjectId")) if payload.get("targetProjectId") else None
    if payload.get("targetProjectId") and not target_project:
        raise ValueError("目标项目不存在或已移除")
    owner = Project.objects.filter(root_path__iexact=str(project_path)).first()
    if target_project and owner and str(owner.id) != target_project['id']:
        raise ValueError("这个目录属于另一个项目，请切换目标项目")
    raw_dir = project_raw_dir({"path": str(project_path), **({"rawDir": payload["rawDir"]} if payload.get("rawDir") else {})})
    source_assignments = validate_source_assignments({"path": str(project_path), "rawDir": str(raw_dir)}, payload.get("sourceAssignments"), payload.get("sourceFiles"), int(payload.get("limitDocs") or 0))
    job_id = uuid.uuid4().hex
    checkpoint_id = checkpoint_id or uuid.uuid4().hex
    model_config = get_llm_model(payload.get("modelId"))
    script_path = Path(__file__).resolve().parents[1] / "tools" / "ingest_project.py"
    command = [
        sys.executable,
        str(script_path),
        "--project-dir",
        str(project_path),
    ]
    ingestion_output = project_path / ".index" / f"ingestion-{job_id}.json"
    command.extend(["--ingestion-output", str(ingestion_output)])
    source_files = payload.get("sourceFiles") or []
    if not isinstance(source_files, list) or any(not isinstance(value, str) or not value.strip() for value in source_files):
        raise ValueError("sourceFiles 必须为原文相对路径列表")
    receipt = payload.get("uploadReceipt")
    if receipt is not None:
        if (not isinstance(receipt, dict) or any(type(receipt.get(key)) is not int or not 0 <= receipt[key] <= 10000
                                                for key in ("chosen", "uploaded", "skipped"))
                or receipt["uploaded"] + receipt["skipped"] > receipt["chosen"]
                or not isinstance(receipt.get("skippedFiles"), list)
                or len(receipt["skippedFiles"]) > 1000
                or any(not isinstance(name, str) or len(name) > 2000 for name in receipt["skippedFiles"])):
            raise ValueError("上传回执统计格式无效")
        receipt = {key: receipt[key] for key in ("chosen", "uploaded", "skipped", "skippedFiles")}
    for source_file in source_files:
        command.extend(["--source-file", source_file.strip()])
    from .file_paths import fs_path
    checkpoint_dir = fs_path(project_path / ".index" / "ingestion-checkpoints" / checkpoint_id)
    command.extend(["--checkpoint-dir", str(checkpoint_dir)])
    prompt_file = checkpoint_dir / "prompts.json"
    command.extend(["--prompt-file", str(prompt_file)])
    batch_chars = int(payload.get("extractionBatchChars", os.getenv("INGEST_BATCH_CHARS", "3000")))
    batch_size = int(payload.get("extractionBatchSize", os.getenv("INGEST_BATCH_SIZE", "6")))
    if not 500 <= batch_chars <= 12000 or not 1 <= batch_size <= 12:
        raise ValueError("抽取批次长度应为500–12000字符，块数应为1–12")
    command.extend(["--extraction-batch-chars", str(batch_chars), "--extraction-batch-size", str(batch_size)])
    command.extend(["--raw-dir", str(raw_dir)])
    if int(payload.get("limitDocs") or 0) > 0:
        command.extend(["--limit-docs", str(int(payload["limitDocs"]))])
    if payload.get("maxItemsPerChunk") is not None:
        command.extend(["--max-items-per-chunk", str(max(1, min(int(payload["maxItemsPerChunk"]), 10)))])
    if payload.get("maxObjects") is not None:
        command.extend(["--max-objects", str(max(1, int(payload["maxObjects"])))])
    if payload.get("llmConcurrency") is not None:
        command.extend(["--llm-concurrency", str(max(1, int(payload["llmConcurrency"])))])
    prompt_profile = payload.get("promptProfile") or os.getenv("INGEST_PROMPT_PROFILE", "compact")
    if prompt_profile not in {"compact", "standard"}:
        raise ValueError("promptProfile 必须为 compact 或 standard")
    command.extend(["--prompt-profile", prompt_profile])
    if payload.get("allowHeuristic"):
        command.append("--allow-heuristic")

    export_dir = Path(payload.get("exportDir") or project_path / "exports" / "wiki").resolve()
    target_keys = ingestion_target_keys(project_path, export_dir)
    with BUILD_JOBS_LOCK:
        conflicting_job_id = next((ACTIVE_INGESTION_TARGETS.get(key) for key in target_keys if ACTIVE_INGESTION_TARGETS.get(key)), None)
        if conflicting_job_id:
            raise IngestionConflict(f"该项目或导出目录已有摄入任务正在运行：{project_path}", conflicting_job_id)
        for key in target_keys:
            ACTIVE_INGESTION_TARGETS[key] = job_id

    request_options = {
        "projectDir": str(project_path),
        "rawDir": str(raw_dir),
        "exportDir": str(export_dir),
        "ingestionOutput": str(ingestion_output),
        "checkpointId": checkpoint_id,
        "modelId": model_config["id"],
        "modelName": model_config.get("name") or model_config["model"],
        "promptProfile": prompt_profile,
        "extractionBatchChars": batch_chars,
        "extractionBatchSize": batch_size,
        "limitDocs": int(payload.get("limitDocs") or 0),
        "sourceFiles": source_files,
        "uploadReceipt": receipt,
        "sourceAssignments": source_assignments,
        "targetProjectId": target_project['id'] if target_project else None,
        "originalProjectPath": target_project['path'] if target_project else None,
        "allowHeuristic": bool(payload.get("allowHeuristic")),
        "projectName": payload.get("projectName") or project_path.name,
        "timeoutSeconds": int(payload.get("timeoutSeconds") or 7200),
    }
    for key in ("maxItemsPerChunk", "maxObjects", "llmConcurrency"):
        if payload.get(key) is not None:
            request_options[key] = int(payload[key])
    job = {
        "id": job_id,
        "status": "running",
        "ok": None,
        "startedAt": time.time(),
        "finishedAt": None,
        "progress": {"current": 0, "total": 0, "percent": 0},
        "projectDir": str(project_path),
        "rawDir": str(raw_dir),
        "exportDir": str(export_dir),
        "ingestionOutput": str(ingestion_output),
        "checkpointId": checkpoint_id,
        "checkpointDir": str(checkpoint_dir),
        "modelId": model_config["id"],
        "modelName": model_config.get("name") or model_config["model"],
        "summary": export_summary(export_dir),
        "logs": [],
        "error": "",
        "failureKind": "",
        "retryable": False,
        "retryAfterSeconds": None,
        "retryOf": retry_of,
        "attempt": attempt,
        "request": request_options,
        "command": command,
        "timeoutSeconds": int(payload.get("timeoutSeconds") or 7200),
        "projectName": payload.get("projectName") or project_path.name,
    }
    from .audit import actor_context
    job["auditActor"] = dict(actor_context.get())
    try:
        from .ingestion_prompts import load_prompts, save_prompts
        if not prompt_file.exists():
            save_prompts(load_prompts(), prompt_file)
        save_job(job)
        thread = threading.Thread(target=run_ingestion_job, args=(job_id, script_path), daemon=True)
        thread.start()
        return job
    except Exception:
        release_ingestion_target(job_id, export_dir, project_path)
        raise


@require_http_methods(["GET"])
@handle_errors
@admin_required
def ingestion_jobs(request):
    try:
        limit = max(1, min(int(request.GET.get("limit") or 100), 500))
    except (TypeError, ValueError):
        limit = 100
    return ok({"jobs": list_ingestion_jobs(limit), "total": count_ingestion_jobs()})


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
@admin_required
def retry_ingestion_job(request, job_id):
    previous = get_job(job_id)
    if not previous:
        return ok({"error": "任务不存在或已过期"}, status=404)
    if previous.get("status") not in {"failed", "rate_limited", "interrupted", "partial_succeeded"}:
        return ok({"error": "只有失败、限流中断或服务中断的任务可以重试"}, status=409)
    payload = dict(previous.get("request") or {})
    if not payload:
        payload = {
            "projectDir": previous.get("projectDir"),
            "rawDir": previous.get("rawDir"),
            "exportDir": previous.get("exportDir"),
            "projectName": previous.get("projectName"),
            "timeoutSeconds": previous.get("timeoutSeconds") or 7200,
        }
    payload.setdefault("promptProfile", "standard")
    if previous.get("status") == "rate_limited":
        previous_concurrency = max(1, int(payload.get("llmConcurrency") or os.getenv("LLM_CONCURRENCY", "4")))
        payload["llmConcurrency"] = max(1, previous_concurrency // 2)
    try:
        project_path = Path(payload.get("projectDir") or previous.get("projectDir") or "").resolve()
        retried = start_ingestion_job(
            payload,
            project_path,
            retry_of=previous["id"],
            attempt=int(previous.get("attempt") or 1) + 1,
            checkpoint_id=previous.get("checkpointId"),
        )
        return ok(retried)
    except IngestionConflict as exc:
        return ok({"error": str(exc), "conflictingJobId": exc.job_id}, status=409)


@require_http_methods(["GET"])
@handle_errors
@admin_required
def ingestion_job(request, job_id):
    job = get_job(job_id)
    if not job:
        return ok({"error": "任务不存在或已过期"}, status=404)
    return ok(job)


def run_ingestion_job(job_id, script_path):
    job = get_job(job_id)
    perf_started = time.perf_counter()
    performance = job["performance"] = {
        "receivedLines": 0, "periodicLogSaveCount": 0, "periodicLogSaveSeconds": 0,
        "periodicLogSaveMaxMs": 0, "modelRequests": 0, "modelErrors": 0, "jsonRepairRequests": 0,
        "maxPipeLagSeconds": 0, "failedChunks": 0, "retryAttempts": 0, "batchFallbacks": 0,
    }
    perf_file = None

    def record_perf(event):
        nonlocal perf_file
        received_at = time.time()
        event = {**event, "receivedAt": received_at}
        if isinstance(event.get("emittedAt"), (int, float)):
            event["pipeLagSeconds"] = round(max(0, received_at - event["emittedAt"]), 3)
            performance["maxPipeLagSeconds"] = max(performance["maxPipeLagSeconds"], event["pipeLagSeconds"])
        if perf_file is not None:
            try:
                perf_file.write(json.dumps({"jobId": job_id, **event}, ensure_ascii=False) + "\n")
            except OSError:
                disable_perf_file()

    def disable_perf_file():
        nonlocal perf_file
        handle, perf_file = perf_file, None
        if handle is not None:
            try:
                handle.close()
            except OSError:
                pass
        append_job_log(job_id, "性能日志文件写入失败，汇总仍保存在任务详情 performance 中", job=job, persist=False)

    def flush_perf_file():
        if perf_file is not None:
            try:
                perf_file.flush()
            except OSError:
                disable_perf_file()

    def receive_line(line):
        nonlocal extraction_failure
        extraction_failure = parse_ingestion_failure(line) or extraction_failure
        performance["receivedLines"] += 1
        append_job_log(job_id, line, job=job, persist=False)
        if line.startswith(("PERF ", "CONVERT ")):
            try:
                event = json.loads(line.split(" ", 1)[1])
            except json.JSONDecodeError:
                return
            if not isinstance(event, dict):
                return
            if line.startswith("CONVERT "):
                event["stage"] = "conversion_" + str(event.get("stage", "unknown"))
                if event["stage"] == "conversion_batch_start":
                    performance["conversion"] = {"total": event.get("total", 0), "current": 0,
                        "failed": 0, "cacheHits": 0, "concurrency": event.get("concurrency"),
                        "converterVersion": event.get("converterVersion")}
                elif event["stage"] in {"conversion_file_done", "conversion_file_failed"}:
                    conversion = performance.setdefault("conversion", {"failed": 0, "cacheHits": 0})
                    conversion.update(current=event.get("current", 0), total=event.get("total", 0),
                                      lastFile=event.get("sourceRef"), lastSeconds=event.get("seconds"))
                    conversion["failed"] += int(event["stage"] == "conversion_file_failed")
                    conversion["cacheHits"] += int(bool(event.get("cached")))
            stage = event.get("stage")
            performance["lastStage"] = stage
            if stage == "llm_request":
                performance["modelRequests"] += 1
                performance["modelErrors"] += int(not event.get("ok"))
            elif stage == "json_repair":
                performance["jsonRepairRequests"] += 1
            elif stage == "chunk_failed":
                performance["failedChunks"] += 1
            elif stage == "chunk_retry":
                performance["retryAttempts"] += 1
            elif stage == "batch_fallback":
                performance["batchFallbacks"] += 1
            record_perf(event)

    def perf_snapshot():
        performance["elapsedSeconds"] = round(time.perf_counter() - perf_started, 3)
        performance["periodicLogSaveWallPercent"] = round(
            performance["periodicLogSaveSeconds"] * 100 / max(performance["elapsedSeconds"], .001), 3)
        record_perf({"stage": "parent_summary", "at": time.time(), "status": job["status"], **performance})
        flush_perf_file()

    env = {**os.environ, "PYTHONIOENCODING": "utf-8"}
    model_config = get_llm_model(job.get("modelId"))
    from .retained_logging import log_secrets
    secrets_token = log_secrets.set((model_config.get("apiKey") or "",))
    env.update({
        "LLM_MODEL": model_config["model"],
        "LLM_API_BASE": model_config.get("apiBase") or "",
        "LLM_API_KEY": model_config.get("apiKey") or "",
    })
    process = None
    rate_limit = None
    extraction_failure = None
    try:
        perf_path = Path(job["projectDir"]) / ".index" / f"ingestion-{job_id}.perf.jsonl"
        job["performanceLog"] = str(perf_path)
        try:
            perf_path.parent.mkdir(parents=True, exist_ok=True)
            perf_file = perf_path.open("a", encoding="utf-8", buffering=65536)
        except OSError:
            disable_perf_file()
        from django.db import connection
        with connection.cursor() as cursor:
            performance["sqliteJournalMode"] = cursor.execute("PRAGMA journal_mode").fetchone()[0]
            performance["sqliteSynchronous"] = cursor.execute("PRAGMA synchronous").fetchone()[0]
        record_perf({"stage": "parent_start", "at": time.time(), **performance})
        process = subprocess.Popen(
            job["command"],
            cwd=str(script_path.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=env,
        )
        deadline = time.time() + job["timeoutSeconds"]
        last_log_save = time.monotonic()
        last_perf_snapshot = last_log_save
        while True:
            line = process.stdout.readline()
            if line:
                clean_line = line.rstrip()
                receive_line(clean_line)
                if time.monotonic() - last_log_save >= 1:
                    if time.monotonic() - last_perf_snapshot >= 10:
                        perf_snapshot()
                        last_perf_snapshot = time.monotonic()
                    flush_perf_file()
                    save_started = time.perf_counter()
                    save_job(job)
                    save_seconds = time.perf_counter() - save_started
                    performance["periodicLogSaveCount"] += 1
                    performance["periodicLogSaveSeconds"] += save_seconds
                    performance["periodicLogSaveMaxMs"] = round(max(performance["periodicLogSaveMaxMs"], save_seconds * 1000), 3)
                    last_log_save = time.monotonic()
                rate_limit = parse_rate_limit_error(clean_line) or rate_limit
                if rate_limit:
                    process.kill()
                    break
            if process.poll() is not None:
                break
            if time.time() > deadline:
                process.kill()
                raise TimeoutError("生成任务超时")
        for line in process.stdout.readlines():
            receive_line(line.rstrip())
        return_code = process.wait()
        job["returnCode"] = return_code
        job["summary"] = export_summary(Path(job["exportDir"]))
        job["finishedAt"] = time.time()
        report_path = fs_path(Path(job["projectDir"]) / ".index" / "ingestion-artifacts" / str(job.get("checkpointId", "")) / "manifest.json")
        if report_path.is_file():
            job["ingestionReport"] = json.loads(report_path.read_text(encoding="utf-8"))
        if rate_limit:
            job["status"] = "rate_limited"
            job["ok"] = False
            job["failureKind"] = "rate_limit"
            job["retryable"] = True
            job["retryAfterSeconds"] = rate_limit.get("retryAfterSeconds")
            job["error"] = rate_limit.get("message") or "模型接口限流，任务已中断，可稍后重试"
        elif return_code == 0:
            publish_started = time.perf_counter()
            record_perf({"stage": "publish_start", "at": time.time()})
            ingestion_path = Path(job["ingestionOutput"])
            extraction = json.loads(ingestion_path.read_text(encoding="utf-8"))
            report = extraction.get("ingestionReport") or {}
            job["ingestionReport"] = report
            assignments = job.get("request", {}).get("sourceAssignments")
            if report:
                successful = {row["originalSource"] for row in report["documents"] if row["status"] == "succeeded" and row.get("isLive", True)}
                assignments = [row for row in (assignments or []) if row["sourceFile"] in successful]
            target_id = job.get("request", {}).get("targetProjectId")
            if target_id:
                existing = get_project(target_id)
                if not existing:
                    raise ValueError("目标项目已移除，无法发布")
                project = {**existing, "path": job["projectDir"], "rawDir": job["rawDir"], "exportDir": job["exportDir"]}
            else:
                existing_project = Project.objects.filter(root_path__iexact=str(Path(job["projectDir"]).resolve())).first()
                project = register_project_directory(
                    job["projectDir"], existing_project.name if existing_project else job.get("projectName"), job.get("rawDir"), job.get("exportDir"), build_index=False
                )
            if extraction.get("documentImport"):
                from .search_index import publish_document_import
                project["searchIndex"] = publish_document_import(
                    project, ingestion_path.with_suffix(".db"),
                    extraction["documentImport"]["baseline"], extraction,
                    source_assignments=assignments,
                )
            else:
                project["searchIndex"] = build_project_index(project, extracted_knowledge=extraction)
            if not project["searchIndex"].get("ok"):
                raise RuntimeError("V7 内容索引构建失败")
            if report:
                from .ingestion_staging import reconcile_publication
                reconcile_publication(project, report)
            if target_id:
                registered = register_ingested_project(job)
                project = {**registered, "searchIndex": project["searchIndex"]}
            record_perf({"stage": "publish_end", "seconds": round(time.perf_counter() - publish_started, 3)})
            export_started = time.perf_counter()
            project["markdownExport"] = export_project_markdown(project)
            record_perf({"stage": "export_end", "seconds": round(time.perf_counter() - export_started, 3)})
            job["status"] = "partial_succeeded" if report.get("failedDocuments") else "succeeded"
            job["retryable"] = bool(report.get("failedDocuments"))
            job["ok"] = True
            job["registeredProject"] = project
            job["summary"] = export_summary(Path(project["exportDir"]))
            total = job.get("progress", {}).get("total") or len(list(Path(job["exportDir"]).glob("sources/**/*.md")))
            job["progress"] = {"current": total, "total": total, "percent": 100}
            checkpoint_dir = job.get("checkpointDir")
            if checkpoint_dir and not job["retryable"]:
                shutil.rmtree(checkpoint_dir, ignore_errors=True)
        else:
            job["status"] = "failed"
            job["ok"] = False
            job["retryable"] = True
            job["error"] = (extraction_failure or {}).get('message') or "知识摄入进程失败"
            job["failureKind"] = (extraction_failure or {}).get('kind') or 'extraction_failed'
        perf_snapshot()
        save_job(job)
    except Exception as exc:
        if process and process.poll() is None:
            process.kill()
        job["status"] = "failed"
        job["ok"] = False
        job["error"] = str(exc)
        job["retryable"] = True
        job["finishedAt"] = time.time()
        perf_snapshot()
        save_job(job)
    finally:
        from .audit import task_finished
        if job:
            task_finished("ingestion", job_id, job.get("status", "interrupted"), job.get("auditActor", {}),
                          (job.get("registeredProject") or {}).get("id") or job.get("request", {}).get("targetProjectId", ""))
        if perf_file is not None:
            try:
                perf_file.close()
            except OSError:
                pass
        if job:
            ingestion_output = job.get("ingestionOutput")
            if ingestion_output:
                Path(ingestion_output).unlink(missing_ok=True)
            release_ingestion_target(job_id, job.get("exportDir"), job.get("projectDir"))
        log_secrets.reset(secrets_token)


@csrf_exempt
@require_http_methods(["GET", "POST"])
@handle_errors
@admin_required
def ingestion_prompt_settings(request):
    from .ingestion_prompts import DEFAULT_PROMPTS, load_prompts, save_prompts
    prompts = save_prompts(json_body(request).get("prompts")) if request.method == "POST" else load_prompts()
    return ok({"prompts": prompts, "defaults": DEFAULT_PROMPTS})


def parse_ingestion_failure(line):
    if line.startswith('TASK_ERROR fatal '):
        try:
            value = json.loads(line[len('TASK_ERROR fatal '):])
            return {'kind': 'fatal', 'message': str(value['message'])[:2000]}
        except (ValueError, KeyError, TypeError):
            return None
    if line.startswith('TASK_ERROR chunks_failed '):
        try:
            value = json.loads(line[len('TASK_ERROR chunks_failed '):])
            return {'kind': 'chunks_failed', 'message': str(value['message'])[:2000]}
        except (ValueError, KeyError, TypeError):
            return None
    prefix = 'TASK_ERROR output_limit '
    if line.startswith(prefix):
        try:
            data = json.loads(line[len(prefix):])
        except (json.JSONDecodeError, TypeError):
            return None
        if isinstance(data, dict) and isinstance(data.get('message'), str):
            return {'kind': 'output_limit', 'message': data['message'][:2000]}
    if line.startswith(('RuntimeError:', 'ExtractionOutputLimitError:')) and 'token' in line.lower() and '上限' in line:
        return {'kind': 'output_limit', 'message': line.split(':', 1)[1].strip()[:2000]}
    return None


def parse_rate_limit_error(line):
    prefix = "TASK_ERROR rate_limit "
    if not str(line).startswith(prefix):
        return None
    try:
        data = json.loads(str(line)[len(prefix):])
        return data if isinstance(data, dict) else {"message": str(data)}
    except json.JSONDecodeError:
        return {"message": str(line)[len(prefix):].strip() or "模型接口限流"}


def ingestion_target_keys(project_dir, export_dir):
    return (
        f"project:{os.path.normcase(str(Path(project_dir).resolve()))}",
        f"export:{os.path.normcase(str(Path(export_dir).resolve()))}",
    )


def release_ingestion_target(job_id, export_dir, project_dir=None):
    if not export_dir or not project_dir:
        return
    with BUILD_JOBS_LOCK:
        for target_key in ingestion_target_keys(project_dir, export_dir):
            if ACTIVE_INGESTION_TARGETS.get(target_key) == job_id:
                ACTIVE_INGESTION_TARGETS.pop(target_key, None)


def append_job_log(job_id, line, job=None, persist=True):
    job = job if job is not None else get_job(job_id)
    if not job:
        return
    if line:
        from .retained_logging import redact
        line = redact(line)
        logging.getLogger("api.tasks").info("%s", line, extra={"task_id": job_id})
        job["logs"].append(line)
        job["logs"] = job["logs"][-500:]
        progress = parse_progress(line)
        if progress:
            job["progress"] = progress
    if persist:
        save_job(job)


def parse_progress(line):
    match = re.search(r"^PROGRESS\s+chunks\s+(\d+)\s+(\d+)\s*$", line)
    if not match:
        return None
    current = int(match.group(1))
    total = int(match.group(2))
    percent = round(current / total * 100) if total else 0
    return {"current": current, "total": total, "percent": percent, "unit": "chunks"}


def get_job(job_id):
    record = IngestionJob.objects.filter(pk=str(job_id)).first()
    if not record:
        return None
    job = dict(record.payload or {})
    job["id"] = record.id
    job["status"] = record.status
    job["startedAt"] = record.started_at
    job["finishedAt"] = record.finished_at
    job["logs"] = list(job.get("logs", []))
    if job['status'] == 'failed' and job.get('error') == '知识摄入进程失败':
        failure = next((data for line in reversed(job['logs']) if (data := parse_ingestion_failure(line))), None)
        if failure:
            job.update(error=failure['message'], failureKind=failure['kind'])
    return job


def list_ingestion_jobs(limit=100):
    jobs = []
    for record in IngestionJob.objects.order_by("-started_at")[:limit]:
        job = dict(record.payload or {})
        job.update({
            "id": record.id,
            "status": record.status,
            "startedAt": record.started_at,
            "finishedAt": record.finished_at,
        })
        jobs.append({
            key: job.get(key)
            for key in (
                "id", "status", "ok", "startedAt", "finishedAt", "progress",
                "projectDir", "rawDir", "exportDir", "projectName", "summary",
                "modelId", "modelName", "ingestionReport",
                "error", "returnCode",
                "failureKind", "retryable", "retryAfterSeconds", "retryOf", "attempt",
            )
        })
    return jobs


def count_ingestion_jobs():
    return IngestionJob.objects.count()


def save_job(job):
    project = None
    target_id = (job.get('request') or {}).get('targetProjectId')
    if target_id:
        project = Project.objects.filter(pk=target_id).first()
    project_dir = str(job.get("projectDir") or "").strip()
    if project is None and project_dir:
        project = Project.objects.filter(root_path__iexact=str(Path(project_dir).resolve())).first()
    IngestionJob.objects.update_or_create(
        id=str(job["id"]),
        defaults={
            "project": project,
            "status": job.get("status") or "unknown",
            "started_at": float(job.get("startedAt") or 0),
            "finished_at": job.get("finishedAt"),
            "payload": job,
        },
    )


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
@admin_required
def register_project(request):
    payload = json_body(request)
    project_dir = (payload.get("projectDir") or "").strip()
    if not project_dir:
        return ok({"error": "缺少项目目录"}, status=400)

    project_path = Path(project_dir).resolve()
    if not project_path.exists() or not project_path.is_dir():
        return ok({"error": f"项目目录不存在：{project_path}"}, status=400)

    registered = Project.objects.filter(root_path__iexact=str(project_path)).first()
    primary = registered.source_roots.filter(is_primary=True).first() if registered else None
    raw_dir = Path(payload.get("rawDir") or primary.path).resolve() if payload.get("rawDir") or primary else project_raw_dir({"path": str(project_path)})
    if not raw_dir.is_dir() and not (project_path / ".index" / "search.db").is_file():
        return ok({"error": f"没有找到原文目录：{raw_dir}"}, status=400)
    export_dir = Path(payload.get("exportDir") or (registered.export_dir if registered else "") or project_path / "exports" / "wiki").resolve()
    name = payload.get("projectName") or (registered.name if registered else project_path.name)
    project = register_project_directory(project_path, name, raw_dir, export_dir, build_index=False)
    return ok(
        {
            "ok": True,
            "registeredProject": project,
            "projectDir": str(project_path),
            "rawDir": str(raw_dir),
            "exportDir": str(export_dir),
            "summary": export_summary(export_dir),
        }
    )


@csrf_exempt
@require_http_methods(["GET", "POST"])
@handle_errors
def search_index(request, project_id):
    project = get_project(project_id)
    if not project:
        return ok({"error": "项目不存在"}, status=404)
    if request.method == "GET":
        return ok({"path": str(project_root(project) / ".index" / "search.db"), **verify_project_index(project)})
    user = request.user
    if not user.is_authenticated or not user.is_staff:
        return ok({"error": "需要管理员权限", "needLogin": True, "needAdmin": True}, status=403)
    payload = json_body(request)
    return ok(build_project_index(project, force=bool(payload.get("force"))))


@require_http_methods(["GET"])
@handle_errors
@admin_required
def index_inspector(request, project_id):
    project = get_project(project_id)
    if not project:
        return ok({"error": "项目不存在"}, status=404)
    return ok(inspect_project_index(
        project,
        document_id=request.GET.get("documentId"),
        chunk_id=request.GET.get("chunkId"),
    ))


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
@admin_required
def export_markdown_view(request, project_id):
    project = get_project(project_id)
    if not project:
        return ok({"error": "项目不存在"}, status=404)
    return ok({"ok": True, "export": export_project_markdown(project)})


def export_summary(export_dir):
    summary = {}
    for folder in ("sources", "objects"):
        target = export_dir / folder
        summary[folder] = len(list(target.rglob("*.md"))) if target.exists() else 0
    summary["hasIndex"] = (export_dir / "index.md").exists()
    summary["hasLog"] = (export_dir / "log.md").exists()
    return summary


@require_http_methods(["GET"])
@handle_errors
def files(request, project_id):
    if not get_project(project_id):
        return ok({"error": "项目不存在"}, status=404)
    return ok(project_files(project_id))


@require_http_methods(["GET"])
@handle_errors
def graph(request, project_id):
    if not get_project(project_id):
        return ok({"error": "项目不存在"}, status=404)
    return ok(project_graph(project_id, json.loads(request.GET.get("versionScope", "{}"))))


@require_http_methods(["GET"])
@handle_errors
def file_content(request, project_id):
    path = request.GET.get("path")
    if not path:
        return ok({"error": "缺少 path 参数"}, status=400)
    if not get_project(project_id):
        return ok({"error": "项目不存在"}, status=404)
    project = get_project(project_id)
    edition_id = request.GET.get("editionId")
    scope = {}
    if edition_id:
        edition = next((version for group in list_editions(project)["documents"] for version in group["versions"] if version["editionId"] == edition_id), None)
        if not edition or edition["path"] != path:
            raise ValueError("文档版本不存在或路径不匹配")
        scope[edition["logicalDocumentId"]] = edition_id
    scoped = scope_project(project, scope)
    cached = get_indexed_document(scoped, path)
    if not cached:
        raise ValueError("文档不在所选版本范围内或尚未入库")
    node_id = request.GET.get("structuralNodeId")
    content = cached["extracted_text"]
    if node_id:
        from .search_index import get_indexed_structural_node
        node = get_indexed_structural_node(scoped, node_id)
        if not node or node.get("path") != path:
            raise ValueError("所选版本的章节不存在或不属于该文档")
        content = node["content"]
    return ok({"path": path, "content": content, "indexed": True,
               "documentType": cached["document_type"],
               **{key: cached[key] for key in ("editionId", "documentName", "versionLabel") if key in cached}})


@csrf_exempt
@require_http_methods(["GET", "POST"])
@handle_errors
def document_versions(request, project_id):
    project = get_project(project_id)
    if not project:
        return ok({"error": "项目不存在"}, status=404)
    if request.method == "GET":
        return ok(list_editions(project))
    if not request.user.is_staff:
        return ok({"error": "需要管理员权限"}, status=403)
    payload = json_body(request)
    if payload.get("action") == "initialize":
        result = build_project_index(project)
        if not result.get("ok"):
            raise ValueError(f"版本索引初始化失败：{result.get('failures')}")
        return ok(list_editions(project))
    return ok(update_edition(project, payload))


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
def search(request, project_id):
    payload = json_body(request)
    query = (payload.get("query") or "").strip()
    if not query:
        return ok({"error": "缺少 query"}, status=400)
    started = time.perf_counter()
    performance = {"requestId": uuid.uuid4().hex, "timingsMs": {}}
    with search_phase(performance, "project_lookup"):
        project = get_project(project_id)
    if not project:
        return ok({"error": "项目不存在"}, status=404)
    with search_phase(performance, "query_enrichment"):
        query_enrichment = build_query_enrichment(project_id, query, payload)
    performance["intentModelMs"] = query_enrichment["queryIntent"].get("modelCallElapsedMs", 0)
    synonym_expansion = query_enrichment["synonymExpansion"]
    local_payload = {**payload, "query": query_enrichment["searchQuery"], "originalQuery": query, "queryIntent": query_enrichment["queryIntent"]}
    with search_phase(performance, "local_retrieval"):
        data = project_search(project_id, local_payload)
    results = normalize_search_results(data)
    terms = build_terms(query, results, [], query_enrichment["expandedTerms"])
    results = [{**item, "highlightTerms": terms} for item in results]
    response_payload = {
            "results": results,
            "versionScope": payload.get("versionScope") or {},
            "synonymExpansion": synonym_expansion,
            "queryIntent": query_enrichment["queryIntent"],
            "searchQuery": query_enrichment["searchQuery"],
            "highlightTerms": terms,
            "searchIndex": data.get("index") if isinstance(data, dict) else None,
            "llmRerank": {"enabled": False, "reason": "本地索引使用确定性排序"},
        }
    with search_phase(performance, "search_context"):
        response_payload["searchContextId"] = store_search_context(
            request, project_id, query, query_enrichment, results, payload.get("versionScope")
        )
    response_payload["queryPlan"] = public_query_plan(query_enrichment, results)
    performance["totalMs"] = round((time.perf_counter() - started) * 1000, 2)
    response_payload["performance"] = performance
    print("SEARCH_PERF " + json.dumps({**performance, "event": "complete", "resultCount": len(results)}), flush=True)
    return ok(response_payload)


@contextmanager
def search_phase(performance, stage):
    record = {"requestId": performance["requestId"], "stage": stage}
    print("SEARCH_PERF " + json.dumps({**record, "event": "start"}), flush=True)
    started = time.perf_counter()
    try:
        yield
    except Exception as exc:
        record["errorType"] = type(exc).__name__
        raise
    finally:
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        performance["timingsMs"][stage] = elapsed
        print("SEARCH_PERF " + json.dumps({**record, "event": "end", "elapsedMs": elapsed}), flush=True)


def build_query_enrichment(project_id, query, payload, model_config=None):
    model_config = model_config if model_config is not None else get_llm_model(payload.get("modelId"))
    cached_intent = payload.get("queryIntent")
    cached_search_query = str(payload.get("searchQuery") or "").strip()
    if isinstance(cached_intent, dict) and cached_search_query:
        cached_synonyms = payload.get("synonymExpansion")
        if not isinstance(cached_synonyms, dict):
            cached_synonyms = {
                "query": query,
                "expandedQuery": cached_search_query,
                "expandedTerms": [],
                "matchedGroups": [],
            }
        expanded_terms = [
            *(cached_intent.get("focusTerms") or []),
            *(cached_intent.get("searchTerms") or []),
            *(cached_synonyms.get("expandedTerms") or []),
        ]
        return {
            "queryIntent": cached_intent,
            "synonymExpansion": cached_synonyms,
            "expandedTerms": dedupe_terms(expanded_terms),
            "searchQuery": cached_search_query,
            "modelConfig": model_config,
            "reused": True,
        }
    query_intent = analyze_query_intent(query, model_config) if payload.get("useIntent", True) else {
        "enabled": False,
        "reason": "已关闭",
        "intent": "general",
        "focusTerms": [],
        "searchTerms": [],
        "rewrittenQuery": query,
        "expandedQuery": query,
        "evidencePreference": "source",
        "model": "",
    }
    intent_query = query_intent.get("expandedQuery") or query
    synonym_expansion = expand_query_with_synonyms(project_id, intent_query) if payload.get("useSynonyms", True) else {
        "query": intent_query,
        "expandedQuery": intent_query,
        "expandedTerms": [],
        "matchedGroups": [],
    }
    expanded_terms = [
        *(query_intent.get("focusTerms") or []),
        *(query_intent.get("searchTerms") or []),
        *(synonym_expansion.get("expandedTerms") or []),
    ]
    search_query = expand_query_with_terms(
        synonym_expansion.get("expandedQuery") or intent_query,
        [
            query,
            query_intent.get("rewrittenQuery") or "",
            *(query_intent.get("focusTerms") or []),
            *(query_intent.get("searchTerms") or []),
            *(synonym_expansion.get("expandedTerms") or []),
        ],
    )
    return {
        "queryIntent": query_intent,
        "synonymExpansion": synonym_expansion,
        "expandedTerms": dedupe_terms(expanded_terms),
        "searchQuery": search_query,
        "modelConfig": model_config,
    }


def dedupe_terms(values):
    terms = []
    seen = set()
    for value in values or []:
        clean = str(value or "").strip()
        if not clean:
            continue
        key = clean.casefold()
        if key in seen:
            continue
        seen.add(key)
        terms.append(clean)
    return terms


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
def compose(request, project_id):
    payload = apply_search_context(request, project_id, json_body(request))
    query = (payload.get("query") or "").strip()
    if not query:
        return ok({"error": "缺少 query"}, status=400)

    prepared = prepare_compose(project_id, payload, query)
    llm_result = answer_with_llm(query, prepared["llmDocuments"], prepared["queryEnrichment"]["modelConfig"], prepared["contextOptions"])
    return ok(compose_response_payload(query, prepared, llm_result))


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
def compose_stream(request, project_id):
    payload = apply_search_context(request, project_id, json_body(request))
    query = (payload.get("query") or "").strip()
    if not query:
        return ok({"error": "缺少 query"}, status=400)

    prepared = prepare_compose(project_id, payload, query)

    def events():
        selected_paths = payload.get("paths") or []
        stage_message = "正在读取已选证据..." if selected_paths else "正在检索并准备证据..."
        yield sse_event("stage", {"message": stage_message})
        initial = compose_response_payload(query, prepared, {"answer": "", "llmConfigured": True, "model": prepared["queryEnrichment"]["modelConfig"].get("model")})
        yield sse_event("meta", initial)
        yield sse_event("stage", {"message": "正在流式生成回答..."})
        for event in stream_answer_with_llm(query, prepared["llmDocuments"], prepared["queryEnrichment"]["modelConfig"], prepared["contextOptions"]):
            yield sse_event(event.get("event", "message"), {key: value for key, value in event.items() if key != "event"})

    response = StreamingHttpResponse(events(), content_type="text/event-stream; charset=utf-8")
    response["Cache-Control"] = "no-cache"
    response["X-Accel-Buffering"] = "no"
    return response


def sse_event(event, data):
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


def prepare_compose(project_id, payload, query, model_config=None, retrieval_results=None):
    context_options = {
        "max_chars": max(1000, min(2000000, int(payload.get("contextMaxChars", 24000)))),
        "reserve_chars": max(0, min(2000000, int(payload.get("contextReserveChars", 2000)))),
    }
    query_enrichment = build_query_enrichment(project_id, query, payload, model_config=model_config)
    synonym_expansion = query_enrichment["synonymExpansion"]
    selected_paths = payload.get("paths") or []
    selected_chunk_ids = payload.get("chunkIds") or []
    search_results = []
    if not selected_paths and not selected_chunk_ids:
        if retrieval_results is None:
            search_payload = {**payload, "query": query_enrichment["searchQuery"], "originalQuery": query, "queryIntent": query_enrichment["queryIntent"]}
            search_data = project_search(project_id, search_payload)
            search_results = normalize_search_results(search_data)
        else:
            search_results = retrieval_results
        terms = build_terms(query, search_results, [], query_enrichment["expandedTerms"])
        search_results = [{**item, "highlightTerms": terms} for item in search_results]
        selected_chunk_ids = [item.get("chunkId") for item in search_results if item.get("chunkId")]
        selected_paths = [path for path in (result_path(item) for item in search_results) if path]
    else:
        terms = build_terms(query, search_results, [], query_enrichment["expandedTerms"])

    documents = []
    project = get_project(project_id)
    if project:
        project = scope_project(project, payload.get("versionScope"))
    if selected_chunk_ids and project:
        for section in get_indexed_section_evidence(
            project, selected_chunk_ids, max_sections=int(payload.get("maxDocs", 8))
        ):
            documents.append({
                "path": section["path"], "content": section["content"], "raw": section,
                "chunkId": section["chunkIds"][0], "chunkIds": section["chunkIds"],
                "sectionId": section["id"], "headingPath": section["heading_path"],
                "complete": section["complete"],
                **{key: section[key] for key in ("editionId", "logicalDocumentId", "documentName", "versionLabel", "documentVersionId") if key in section},
            })
        # Validate all selected chunks separately; maxDocs may intentionally limit expansion.
        from .search_index import get_indexed_chunks
        valid_ids = {row["id"] for row in get_indexed_chunks(project, selected_chunk_ids)}
        if any(int(value) not in valid_ids for value in selected_chunk_ids):
            raise ValueError("所选证据不属于当前版本或已失效，请重新检索")
    else:
        for path in selected_paths[: int(payload.get("maxDocs", 8))]:
            raw = get_indexed_document(project, path) if project else None
            if raw is None:
                raise ValueError("所选文档不属于当前版本或尚未入库，请重新检索")
            documents.append({"path": path, "content": raw["extracted_text"], "raw": raw,
                              **{key: raw[key] for key in ("editionId", "documentName", "versionLabel") if key in raw}})
    for document in documents:
        if document.get("versionLabel"):
            document["content"] = f"[文档：{document['documentName']}；版本：{document['versionLabel']}]\n" + document["content"]
    return {
        "documents": documents,
        "targetedEvidence": [],
        "searchResults": search_results,
        "synonymExpansion": synonym_expansion,
        "queryEnrichment": query_enrichment,
        "llmDocuments": documents,
        "contextOptions": context_options,
    }


def compose_response_payload(query, prepared, llm_result):
    return {
        "query": query,
        "documents": [
            {
                "path": item["path"],
                "chars": len(item["content"]),
                **{key: item[key] for key in ("editionId", "logicalDocumentId", "documentName", "versionLabel", "documentVersionId") if key in item},
                **({"chunkId": item["chunkId"]} if item.get("chunkId") else {}),
                **({"chunkIds": item["chunkIds"]} if item.get("chunkIds") else {}),
                **({"sectionId": item["sectionId"]} if item.get("sectionId") else {}),
                **({"headingPath": item["headingPath"]} if item.get("headingPath") else {}),
                **({"complete": item["complete"]} if "complete" in item else {}),
            }
            for item in prepared["documents"]
        ],
        "targetedEvidence": [
            {"path": item["sourcePath"], "chars": item["chars"], "kind": item["kind"]} for item in prepared["targetedEvidence"]
        ],
        "searchResults": prepared["searchResults"],
        "synonymExpansion": prepared["synonymExpansion"],
        "queryIntent": prepared["queryEnrichment"]["queryIntent"],
        "searchQuery": prepared["queryEnrichment"]["searchQuery"],
        **llm_result,
    }


def maybe_rerank_results(query, results, payload):
    if not payload.get("rerankWithLlm", False):
        return {"enabled": False, "reason": "未启用", "results": results}
    try:
        threshold = float(payload.get("rerankThreshold", 0.45))
    except (TypeError, ValueError):
        threshold = 0.45
    threshold = min(max(threshold, 0), 1)
    return rerank_search_results_with_llm(query, results, threshold, get_llm_model(payload.get("modelId")))


@csrf_exempt
@require_http_methods(["GET", "PUT", "POST"])
@handle_errors
def synonyms(request, project_id):
    if request.method == "GET":
        data = load_synonyms(project_id)
        return ok({"synonyms": data, "yaml": dump_synonyms(data)})

    payload = json_body(request)
    if "yaml" in payload:
        parsed = parse_synonym_text(payload.get("yaml") or "")
    else:
        parsed = payload.get("synonyms") or {}
    saved = save_synonyms(project_id, parsed)
    return ok({"synonyms": saved})


@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
def import_synonyms(request, project_id):
    payload = json_body(request)
    imported = parse_synonym_text(payload.get("text") or "")
    if payload.get("merge", True):
        saved = save_synonyms(project_id, merge_synonyms(load_synonyms(project_id), imported))
    else:
        saved = save_synonyms(project_id, imported)
    return ok({"imported": imported, "synonyms": saved})
@csrf_exempt
@require_http_methods(["POST"])
@handle_errors
def login_view(request):
    payload = json_body(request)
    username = (payload.get("username") or "").strip()
    password = payload.get("password") or ""
    if not username or not password:
        return ok({"error": "请输入用户名和密码"}, status=400)
    user = authenticate(request, username=username, password=password)
    if user is None or not user.is_active:
        return ok({"error": "用户名或密码错误"}, status=401)
    auth_login(request, user)
    return ok(auth_payload(user))


@csrf_exempt
@require_http_methods(["POST"])
def logout_view(request):
    auth_logout(request)
    return ok({"ok": True})


@require_http_methods(["GET"])
def auth_me(request):
    user = request.user
    if user.is_authenticated:
        return ok(auth_payload(user))
    return ok({"authenticated": False, "isAdmin": False, "username": ""})
