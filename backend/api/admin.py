from django.contrib import admin

from .models import AuditEvent, MaintenanceState


class ReadOnlyAdmin(admin.ModelAdmin):
    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False


@admin.register(AuditEvent)
class AuditEventAdmin(ReadOnlyAdmin):
    list_display = ("created_at", "actor", "operation", "target", "outcome", "task_id", "source_ip")
    list_filter = ("action", "result", "created_at")
    search_fields = ("actor", "actor_id", "target", "task_id")
    date_hierarchy = "created_at"

    @admin.display(description="动作", ordering="action")
    def operation(self, obj):
        labels = {
            "login_view": "登录", "logout_view": "退出登录", "projects": "项目管理",
            "register_project": "登记项目", "llm_models": "模型配置", "ingestion_prompt_settings": "提示词配置",
            "upload_ingestion": "上传文件", "build_ingestion": "提交摄入", "retry_ingestion_job": "重试摄入",
            "search_index": "索引维护", "document_versions": "文档版本管理", "export_markdown_view": "导出资料",
            "synonyms": "同义词维护", "import_synonyms": "导入同义词", "cases": "评测题目维护",
            "bulk": "批量维护评测", "imports": "导入评测", "transfer": "迁移评测", "commit": "提交评测变更",
            "runs": "提交评测", "run_detail": "评测任务操作", "review": "评测复核", "report": "导出评测报告",
            "draft_report": "导出预检报告", "ingestion": "摄入任务结束", "evaluation": "评测任务结束",
            "maintenance.started": "开始自动维护", "maintenance.finished": "自动维护结束",
        }
        return labels.get(obj.action, labels.get(obj.action.split(".")[0], obj.action))

    @admin.display(description="结果", ordering="result")
    def outcome(self, obj):
        return {"attempted": "已发起/结果待确认", "succeeded": "成功", "failed": "失败", "denied": "拒绝",
                "completed": "完成", "cancelled": "已取消", "interrupted": "中断", "rate_limited": "限流"}.get(obj.result, obj.result)


@admin.register(MaintenanceState)
class MaintenanceStateAdmin(ReadOnlyAdmin):
    list_display = ("name", "last_success", "next_attempt", "lease_until", "error")
