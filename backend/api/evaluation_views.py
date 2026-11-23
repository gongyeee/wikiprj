import json
import logging
import math
from functools import wraps

from django.core.exceptions import ObjectDoesNotExist, ValidationError
from django.db import IntegrityError, OperationalError, transaction
from django.db.models import Q
from django.db.models.deletion import ProtectedError
from django.http import HttpResponse, JsonResponse
from django.middleware.csrf import get_token
from django.utils import timezone
from django.views.decorators.http import require_http_methods

from .evaluation_data import (FIELDS, case_payload, commit_draft, document_catalog, filtered_cases,
                              import_preview, match_references, question_key, selected_cases, transfer_preview, validate_case)
from .evaluation_excel import read_upload, report_bytes, template_bytes
from .evaluation_runner import (ACTIVE, create_run, expire_stale_runs, retry_results, run_config, summary_for)
from .llm_models import list_llm_models
from .models import EvaluationCase, EvaluationDraft, EvaluationResult, EvaluationRun, Project
from .views import admin_required, json_body as read_json_body

logger = logging.getLogger(__name__)


def json_body(request):
    data = read_json_body(request)
    if not isinstance(data, dict):
        raise ValueError("请求必须为 JSON 对象")
    return data


def response(data, status=200):
    return JsonResponse(data, status=status, json_dumps_params={"ensure_ascii": False})


def endpoint(methods):
    def decorate(fn):
        @require_http_methods(methods)
        @admin_required
        @wraps(fn)
        def wrapped(request, *args, **kwargs):
            try:
                return fn(request, *args, **kwargs)
            except ObjectDoesNotExist:
                return response({"error": "记录不存在或不属于当前项目"}, 404)
            except (ValueError, TypeError, KeyError, ValidationError) as exc:
                return response({"error": str(exc)}, 400)
            except IntegrityError:
                return response({"error": "记录冲突：问题可能重复，或当前项目已有运行中的评测，请刷新"}, 409)
            except OperationalError as exc:
                logger.exception("Evaluation database operation failed: %s", fn.__name__)
                detail = str(exc).lower()
                if "no such table" in detail or "no such column" in detail:
                    return response({"error": "评测数据库结构未就绪，请管理员在当前服务的 backend 目录执行 python manage.py migrate 后重试", "code": "database_schema_not_ready"}, 503)
                if "locked" in detail or "busy" in detail:
                    return response({"error": "数据库正忙，请稍后重试", "code": "database_busy"}, 503)
                return response({"error": "数据库操作失败，请管理员检查后台日志", "code": "database_operation_failed"}, 500)
            except ProtectedError:
                return response({"error": "记录仍被引用，不能删除"}, 409)
        return wrapped
    return decorate


def attachment(content, filename):
    result = HttpResponse(content, content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    result["Content-Disposition"] = f'attachment; filename="{filename}"'
    result["Cache-Control"] = "no-store"
    return result


@endpoint(["GET"])
def config(request):
    expire_stale_runs()
    return response({"fields": FIELDS, "models": list_llm_models(), "csrfToken": get_token(request)})


@endpoint(["GET"])
def template(request):
    return attachment(template_bytes(), "qa-import-template.xlsx")


@endpoint(["GET", "POST"])
def cases(request, project_id):
    project = Project.objects.get(pk=project_id, status="active")
    if request.method == "GET":
        qs = filtered_cases(project.pk, request.GET)
        page = max(1, int(request.GET.get("page", 1)))
        size = min(100, max(1, int(request.GET.get("page_size", 20))))
        all_cases = EvaluationCase.objects.filter(project=project)
        return response({"items": [case_payload(c) for c in qs[(page - 1) * size:page * size]], "total": qs.count(), "page": page,
                         "categories": sorted(set(all_cases.values_list("data__category", flat=True)) - {None, ""}),
                         "scenes": sorted(set(all_cases.values_list("data__scene", flat=True)) - {None, ""})})
    payload = json_body(request)
    data = validate_case(payload)
    data["reference_check"] = match_references(data, document_catalog(project.pk))
    with transaction.atomic():
        if payload.get("id"):
            case = EvaluationCase.objects.get(pk=payload["id"], project=project)
            if case.revision != int(payload.get("revision", 0)):
                return response({"error": "记录已被修改，请刷新后再编辑"}, 409)
            case.revision += 1
            case.data = {**case.data, **data}
            case.question_key = question_key(data["question"])
            case.updated_by = request.user.username
            case.save()
        else:
            case = EvaluationCase.objects.create(project=project, data=data, question_key=question_key(data["question"]), created_by=request.user.username, updated_by=request.user.username)
    return response({"item": case_payload(case)})


@endpoint(["POST"])
def bulk(request, project_id):
    payload = json_body(request)
    with transaction.atomic():
        qs = selected_cases(project_id, payload)
        count = qs.count()
        if payload.get("action") == "delete":
            if payload.get("confirmed_count") != count:
                raise ValueError("删除数量已变化，请重新确认")
            qs.delete()
        elif payload.get("action") == "update":
            changes = payload.get("changes") or {}
            if not changes or set(changes) - {"category", "scene", "status"}:
                raise ValueError("批量修改仅支持分类、场景和状态")
            for case in qs:
                clean = validate_case({**case.data, **changes})
                case.data = {**case.data, **clean}
                case.updated_by = request.user.username
                case.revision += 1
                case.save()
        else:
            raise ValueError("不支持的批量操作")
    return response({"count": count})


@endpoint(["POST"])
def imports(request, project_id):
    Project.objects.get(pk=project_id, status="active")
    if request.content_type == "multipart/form-data":
        upload = request.FILES.get("file")
        sheet = request.POST.get("sheet") or None
        header_row = int(request.POST.get("header_row", 1))
        mapping = json.loads(request.POST["mapping"]) if request.POST.get("mapping") else None
        try:
            data = read_upload(upload, sheet, header_row, mapping)
        except (ValueError, KeyError, TypeError):
            raise
        except Exception:
            raise ValueError("无法读取工作簿，请确认是有效的 .xlsx 文件")
        if isinstance(data, dict):
            return response(data)
        return response(import_preview(request.user, project_id, data, request.POST.get("conflict", "skip")))
    payload = json_body(request)
    return response(import_preview(request.user, project_id, payload.get("rows"), payload.get("conflict", "skip")))


@endpoint(["POST"])
def transfer(request, project_id):
    return response(transfer_preview(request.user, project_id, json_body(request)))


@endpoint(["POST"])
def commit(request, draft_id):
    return response(commit_draft(request.user, draft_id))


@endpoint(["GET"])
def draft_report(request, draft_id):
    from openpyxl import Workbook
    from .evaluation_excel import literal_row, style_sheet, text_parts, workbook_bytes
    draft = EvaluationDraft.objects.get(pk=draft_id, owner_id=request.user.pk)
    wb = Workbook()
    ws = wb.active
    ws.title = "批量处理明细"
    literal_row(ws, ["工作表", "行号/ID", "操作", "问题", "错误或引用警告"])
    for entry in draft.payload["plan"]:
        data = entry.get("data", {})
        problem = entry.get("error") or json.dumps(data.get("reference_check", {}), ensure_ascii=False)
        questions, issues = text_parts(data.get("question", "")), text_parts(problem)
        for i in range(max(len(questions), len(issues))):
            literal_row(ws, [entry.get("sheet", ""), entry.get("row", entry.get("id")), entry["action"], questions[i] if i < len(questions) else "", issues[i] if i < len(issues) else ""])
    style_sheet(ws, [24, 40, 20, 70, 80])
    return attachment(workbook_bytes(wb), "qa-batch-details.xlsx")


def run_payload(run, include_summary=True):
    return {"id": str(run.id), "project_id": str(run.project_id) if run.project_id else None, "project_name": run.project_name,
            "status": run.status, "cancel_requested": run.cancel_requested, "created_at": run.created_at.isoformat(),
            "finished_at": run.finished_at.isoformat() if run.finished_at else None, "created_by": run.created_by, "error": run.error,
            "config": run.config, "summary": summary_for(run, list(run.results.defer("answer", "evidence"))) if include_summary else None}


@endpoint(["GET", "POST"])
def runs(request, project_id):
    expire_stale_runs()
    project = Project.objects.get(pk=project_id, status="active")
    if request.method == "GET":
        qs = EvaluationRun.objects.filter(project=project)
        page = max(1, int(request.GET.get("page", 1)))
        return response({"items": [run_payload(r) for r in qs[(page - 1) * 10:page * 10]], "total": qs.count()})
    payload = json_body(request)
    qs = selected_cases(project_id, payload).filter(data__status="enabled")
    snapshots = [(c.id, {**c.data, "case_revision": c.revision, "project_id": str(c.project_id)}) for c in qs]
    run = create_run(request.user, project, snapshots, run_config(payload.get("config") or {}))
    return response({"run": run_payload(run)}, 201)


def result_payload(result):
    return {"id": result.pk, "case_id": str(result.case_id), "snapshot": result.snapshot, "status": result.status,
            "answer": result.answer, "evidence": result.evidence, "score": result.score, "metrics": result.metrics,
            "error": result.error, "review": result.review, "review_revision": result.review_revision}


@endpoint(["GET", "POST"])
def run_detail(request, run_id):
    expire_stale_runs()
    run = EvaluationRun.objects.get(pk=run_id)
    if request.method == "GET":
        qs = run.results.all()
        if request.GET.get("status"):
            qs = qs.filter(status=request.GET["status"])
        if request.GET.get("review") == "pending":
            qs = qs.filter(Q(review__status__isnull=True) | Q(review__status__in=["pending", "rerun"]))
        if request.GET.get("q"):
            qs = qs.filter(snapshot__question__icontains=request.GET["q"])
        page = max(1, int(request.GET.get("page", 1)))
        return response({"run": run_payload(run), "items": [result_payload(r) for r in qs[(page - 1) * 20:page * 20]], "total": qs.count()})
    payload = json_body(request)
    if payload.get("action") == "cancel":
        with transaction.atomic():
            cancelled = EvaluationRun.objects.filter(pk=run.pk, status__in=ACTIVE).update(
                cancel_requested=True, status="cancelled", finished_at=timezone.now())
            if cancelled:
                run.results.filter(status__in=["pending", "running"]).update(status="cancelled")
        return response({"message": "任务已取消，可选择其他模型重新评测。已发出的模型请求可能仍在服务端执行，返回结果不会写入本任务。"})
    if payload.get("action") == "retry":
        if run.status in ACTIVE:
            raise ValueError("任务运行中，请先取消或等待结束")
        if not run.project_id:
            raise ValueError("原项目已不存在")
        snapshots = [(r.case_id, r.snapshot) for r in retry_results(run)]
        new_run = create_run(request.user, run.project, snapshots, run_config(run.config), retry_of=run.id)
        return response({"run": run_payload(new_run)}, 201)
    raise ValueError("未知任务操作")


@endpoint(["POST"])
def review(request, run_id, result_id):
    payload = json_body(request)
    with transaction.atomic():
        result = EvaluationResult.objects.get(pk=result_id, run_id=run_id)
        if result.status in {"pending", "running"}:
            raise ValueError("题目尚未完成，暂不能复核")
        if result.review_revision != int(payload.get("revision", -1)):
            return response({"error": "复核记录已更新，请刷新后重试"}, 409)
        status = payload.get("status")
        if status not in {"pending", "accepted", "adjusted", "rerun"}:
            raise ValueError("审查结论无效")
        comment = str(payload.get("comment") or "").strip()
        if status in {"adjusted", "rerun"} and not comment:
            raise ValueError("调整评分或要求重跑时，请填写理由")
        if len(comment) > 10000:
            raise ValueError("审查意见最多 10000 字符")
        score, passed = None, None
        if status == "accepted":
            if result.status != "scored":
                raise ValueError("没有自动评分，不能标记评分合理")
            score, passed = result.score["total"], result.score["passed"]
        elif status == "adjusted":
            score = float(payload.get("score"))
            if not math.isfinite(score) or not 0 <= score <= 100 or type(payload.get("passed")) is not bool:
                raise ValueError("人工评分需为 0～100，且需明确是否通过")
            passed = payload["passed"]
        entry = {"status": status, "score": score, "passed": passed, "comment": comment, "by": request.user.username, "at": timezone.now().isoformat()}
        result.review = {**entry, "history": [*result.review.get("history", []), entry]}
        result.review_revision += 1
        result.save(update_fields=["review", "review_revision"])
    return response({"item": result_payload(result)})


@endpoint(["GET"])
def report(request, run_id):
    expire_stale_runs()
    with transaction.atomic():
        run = EvaluationRun.objects.get(pk=run_id)
        results = list(run.results.all())
    return attachment(report_bytes(run, results, summary_for(run, results)), f"qa-review-{run.id}.xlsx")
