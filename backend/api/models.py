import uuid

from django.db import models


class Project(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=200)
    root_path = models.TextField(unique=True)
    export_dir = models.TextField(blank=True)
    status = models.CharField(max_length=24, default="active")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("name", "id")


class ProjectSourceRoot(models.Model):
    project = models.ForeignKey(Project, related_name="source_roots", on_delete=models.CASCADE)
    path = models.TextField()
    is_primary = models.BooleanField(default=True)
    include_patterns = models.JSONField(default=list, blank=True)
    exclude_patterns = models.JSONField(default=list, blank=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=("project", "path"), name="unique_project_source_root"),
        ]


class IngestionJob(models.Model):
    id = models.CharField(primary_key=True, max_length=64)
    project = models.ForeignKey(Project, null=True, blank=True, on_delete=models.SET_NULL)
    status = models.CharField(max_length=32, db_index=True)
    started_at = models.FloatField(db_index=True)
    finished_at = models.FloatField(null=True, blank=True)
    payload = models.JSONField(default=dict)

    class Meta:
        ordering = ("-started_at",)


class EvaluationCase(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, on_delete=models.PROTECT, related_name="evaluation_cases")
    question_key = models.CharField(max_length=64)
    data = models.JSONField(default=dict)
    revision = models.PositiveIntegerField(default=1)
    created_by = models.CharField(max_length=150)
    updated_by = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ("-updated_at", "id")
        constraints = [models.UniqueConstraint(fields=("project", "question_key"), name="unique_evaluation_question")]


class EvaluationRun(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    project = models.ForeignKey(Project, null=True, on_delete=models.SET_NULL)
    project_name = models.CharField(max_length=200)
    config = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default="queued", db_index=True)
    cancel_requested = models.BooleanField(default=False)
    created_by = models.CharField(max_length=150)
    created_at = models.DateTimeField(auto_now_add=True)
    heartbeat_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True)
    error = models.TextField(blank=True)

    class Meta:
        ordering = ("-created_at",)
        constraints = [models.UniqueConstraint(fields=("project",), condition=models.Q(status__in=["queued", "running"]), name="one_active_evaluation_per_project")]


class EvaluationResult(models.Model):
    run = models.ForeignKey(EvaluationRun, on_delete=models.CASCADE, related_name="results")
    case_id = models.UUIDField()
    snapshot = models.JSONField(default=dict)
    status = models.CharField(max_length=24, default="pending")
    answer = models.TextField(blank=True)
    evidence = models.JSONField(default=dict)
    score = models.JSONField(default=dict)
    metrics = models.JSONField(default=dict)
    error = models.TextField(blank=True)
    review = models.JSONField(default=dict)
    review_revision = models.PositiveIntegerField(default=0)

    class Meta:
        ordering = ("id",)


class EvaluationDraft(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    owner_id = models.PositiveIntegerField()
    action = models.CharField(max_length=24)
    payload = models.JSONField(default=dict)
    expires_at = models.DateTimeField()
    committed_at = models.DateTimeField(null=True)


class AuditEvent(models.Model):
    created_at = models.DateTimeField(auto_now_add=True, db_index=True, verbose_name="时间")
    actor_id = models.CharField(max_length=64, blank=True, verbose_name="用户ID")
    actor = models.CharField(max_length=150, blank=True, verbose_name="操作人")
    action = models.CharField(max_length=100, db_index=True, verbose_name="动作")
    target = models.CharField(max_length=250, blank=True, verbose_name="对象")
    result = models.CharField(max_length=24, db_index=True, verbose_name="结果")
    source_ip = models.GenericIPAddressField(null=True, blank=True, verbose_name="来源IP")
    task_id = models.CharField(max_length=64, blank=True, db_index=True, verbose_name="任务ID")
    details = models.JSONField(default=dict, blank=True, verbose_name="摘要")

    class Meta:
        ordering = ("-created_at", "-id")
        verbose_name = "操作审计"
        verbose_name_plural = "操作审计"
        default_permissions = ("view",)


class MaintenanceState(models.Model):
    # One coordination row, shared by all web workers; not a second event log.
    name = models.CharField(primary_key=True, max_length=32)
    owner = models.CharField(max_length=64, blank=True)
    lease_until = models.DateTimeField(null=True)
    last_success = models.DateTimeField(null=True)
    next_attempt = models.DateTimeField(null=True)
    error = models.CharField(max_length=250, blank=True)
