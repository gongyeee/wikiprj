"""Small, content-free operation audit; requests are recorded before mutation."""
import ipaddress
import json
import logging
from contextvars import ContextVar

from django.http import JsonResponse
from django.db import DatabaseError, connection, transaction
from django.urls import Resolver404, resolve

from .models import AuditEvent

logger = logging.getLogger(__name__)
actor_context = ContextVar("audit_actor", default={})
MUTATIONS = {
    "projects", "register_project", "llm_models", "ingestion_prompt_settings",
    "upload_ingestion", "build_ingestion", "retry_ingestion_job", "search_index",
    "document_versions", "export_markdown_view", "synonyms", "import_synonyms",
    "cases", "bulk", "imports", "transfer", "commit", "runs", "run_detail", "review",
}


def actor_snapshot(request):
    user = request.user
    try:
        ip = str(ipaddress.ip_address(request.META.get("REMOTE_ADDR", "")))
    except ValueError:
        ip = None
    return {"actor_id": str(user.pk) if user.is_authenticated else "",
            "actor": user.get_username()[:150] if user.is_authenticated else "anonymous",
            "source_ip": ip}


def record_event(action, result, *, actor=None, target="", task_id="", details=None):
    # A savepoint prevents an audit error from poisoning a caller's transaction.
    with transaction.atomic():
        return AuditEvent.objects.create(**(actor if actor is not None else actor_context.get()),
                                         action=action[:100], result=result[:24], target=str(target)[:250],
                                         task_id=str(task_id)[:64], details=details or {})


def audit_failure(exc, action):
    message = str(exc).lower()
    code = "audit_unavailable"
    explanation = "审计记录写入失败，请管理员检查后端日志后重试"
    if isinstance(exc, DatabaseError):
        if any(text in message for text in ("no such table", "no such column", "does not exist", "undefined table")):
            code = "audit_schema_not_ready"
            explanation = "审计数据库尚未升级，请管理员在当前运行版本的 backend 目录执行 python manage.py migrate，然后重启后端"
        elif "locked" in message or "busy" in message:
            code = "audit_database_busy"
            explanation = "审计数据库正忙，本次操作尚未执行，请稍后重试"
        elif any(text in message for text in ("readonly", "read-only", "permission denied", "unable to open database")):
            code = "audit_database_not_writable"
            explanation = "审计数据库无法写入，请管理员检查数据库文件及其所在目录的写入权限"
        elif "disk is full" in message or "disk full" in message:
            code = "audit_storage_full"
            explanation = "审计数据库存储空间不足，请管理员释放空间后重试"
    # Record classification and the actual database path; omit raw exceptions,
    # which can contain SQL parameters or credentials.
    logger.error("Audit write failed: action=%s code=%s exception=%s database=%s",
                 action, code, type(exc).__name__, connection.settings_dict["NAME"])
    response = JsonResponse({"error": explanation, "code": code}, status=503)
    if code == "audit_database_busy":
        response["Retry-After"] = "2"
    return response


def task_finished(kind, task_id, status, actor, target=""):
    # Include terminal state, never exception text, prompts, answers or credentials.
    try:
        record_event(f"{kind}.finished", status, actor=actor, target=target, task_id=task_id)
    except Exception:
        logger.error("Task audit write failed: kind=%s task=%s", kind, task_id)


class AuditMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Also starts in forked workers when a server preloads WSGI in its master.
        from .maintenance import start_scheduler
        start_scheduler()
        try:
            match = resolve(request.path_info)
        except Resolver404:
            return self.get_response(request)
        actor = actor_snapshot(request)
        token = actor_context.set(actor)
        event = None
        try:
            name = match.func.__name__
            admin_write = request.path_info.startswith("/admin/") and request.method == "POST"
            login = name in {"login_view", "logout_view"} or (admin_write and match.url_name in {"login", "logout"})
            mutation = request.method in {"POST", "PUT", "PATCH", "DELETE"} and name in MUTATIONS
            download = name in {"report", "draft_report"} and request.method == "GET"
            action = f"{name}.{request.method.lower()}"
            if admin_write:
                action = f"admin.{match.url_name}"
            target = next((str(match.kwargs[k]) for k in ("project_id", "run_id", "draft_id", "object_id", "job_id") if k in match.kwargs), "")
            # Only inspect small JSON operations. Never load multipart uploads for auditing.
            data = {}
            if (login or mutation) and request.content_type == "application/json" and int(request.META.get("CONTENT_LENGTH") or 0) <= 65536:
                try:
                    value = json.loads(request.body)
                    data = value if isinstance(value, dict) else {}
                except (ValueError, UnicodeError):
                    pass
            if name == "login_view":
                actor = {**actor, "actor": str(data.get("username", ""))[:150]}
            if admin_write and match.url_name == "login":
                actor = {**actor, "actor": str(request.POST.get("username", ""))[:150]}
            target = target or str(data.get("projectId") or data.get("id") or data.get("modelId") or "")[:250]
            details = {"method": request.method, "fields": sorted(str(k)[:80] for k in data)[:100]}
            if login or mutation or download or admin_write:
                try:
                    event = record_event(action, "attempted", actor=actor, target=target, details=details)
                except Exception as exc:
                    return audit_failure(exc, action)
            response = self.get_response(request)
            result = "denied" if response.status_code in {401, 403} else "failed" if response.status_code >= 400 else "succeeded"
            if login and response.status_code == 401:
                result = "failed"
            response_data = {}
            if event and isinstance(response, JsonResponse):
                try:
                    value = json.loads(response.content)
                    response_data = value if isinstance(value, dict) else {}
                    if response_data.get("ok") is False or response_data.get("error"):
                        result = "failed" if result == "succeeded" else result
                except ValueError:
                    pass
            if admin_write and getattr(response, "context_data", None):
                form = response.context_data.get("adminform") or response.context_data.get("form")
                if form and getattr(form, "form", form).errors:
                    result = "failed"
            if event:
                if (name == "login_view" or (admin_write and match.url_name == "login")) and request.user.is_authenticated:
                    actor = actor_snapshot(request)
                event.result = result
                event.actor_id, event.actor = actor["actor_id"], actor["actor"]
                event.task_id = str(response_data.get("id", ""))[:64] if name in {"build_ingestion", "retry_ingestion_job", "runs"} else ""
                registered = response_data.get("registeredProject") or response_data.get("target") or {}
                if not isinstance(registered, dict):
                    registered = {}
                event.target = event.target or str(registered.get("id") or registered.get("name") or "")[:250]
                event.details = {**details, "http_status": response.status_code}
                try:
                    with transaction.atomic():
                        event.save(update_fields=["result", "actor_id", "actor", "task_id", "target", "details"])
                except Exception:
                    logger.error("Audit finalization failed: event=%s", event.pk)
                    response["X-Audit-Status"] = "pending"
            elif result == "denied" and request.path_info.startswith(("/api/", "/admin/")):
                try:
                    record_event(action, result, actor=actor, target=target, details={"http_status": response.status_code})
                except Exception:
                    logger.error("Permission denial audit write failed: %s", action)
            return response
        finally:
            actor_context.reset(token)
