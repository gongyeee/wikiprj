"""In-service daily retention, with a renewable database lease across workers."""
import gzip
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
import uuid
from datetime import datetime, time as day_time, timedelta
from itertools import groupby
from pathlib import Path

from django.conf import settings
from django.core.serializers.json import DjangoJSONEncoder
from django.db import close_old_connections, transaction
from django.db.models import Q
from django.utils import timezone

from .audit import record_event
from .models import AuditEvent, EvaluationRun, IngestionJob, MaintenanceState

logger = logging.getLogger(__name__)
FILE_PATTERN = re.compile(r"^(runtime|tasks)-(\d{4}-\d{2}-\d{2})-\d+-[a-f0-9]{8}\.jsonl$")
AUDIT_PATTERN = re.compile(r"^audit-(\d{4}-\d{2}-\d{2})\.jsonl\.gz$")
_start_lock = threading.Lock()
_started_pid = None


def scheduled_time(now):
    local = timezone.localtime(now)
    scheduled = timezone.make_aware(datetime.combine(local.date(), day_time(settings.MAINTENANCE_HOUR)))
    return scheduled if now >= scheduled else scheduled - timedelta(days=1)


class Lease:
    def __init__(self, owner):
        self.owner = owner
        self.last_check = 0

    def check(self, force=False):
        if force or time.monotonic() - self.last_check >= 20:
            now = timezone.now()
            if not MaintenanceState.objects.filter(name="daily", owner=self.owner, lease_until__gt=now).update(
                    lease_until=now + timedelta(minutes=5)):
                raise RuntimeError("Maintenance lease lost")
            self.last_check = time.monotonic()


def digest(path, lease):
    result = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            lease.check()
            result.update(chunk)
    return result.digest()


def backup_log(source, destination, lease):
    destination.parent.mkdir(parents=True, exist_ok=True)
    temp = destination.with_name(destination.name + f".{lease.owner}.tmp")
    try:
        with source.open("rb") as original, temp.open("wb") as output:
            while chunk := original.read(1024 * 1024):
                lease.check()
                output.write(chunk)
            output.flush()
            os.fsync(output.fileno())
        if digest(source, lease) != digest(temp, lease):
            raise OSError("Log backup verification failed")
        lease.check(force=True)
        os.replace(temp, destination)
    finally:
        temp.unlink(missing_ok=True)


def maintain(lease, now):
    logs = Path(settings.RETAINED_LOG_DIR).resolve()
    backups = Path(settings.MAINTENANCE_BACKUP_DIR).resolve()
    if logs == backups or logs in backups.parents or backups in logs.parents:
        raise ValueError("Log and backup directories must be separate, non-nested directories")
    logs.mkdir(parents=True, exist_ok=True)
    backups.mkdir(parents=True, exist_ok=True)
    today = timezone.localdate(now)
    audit_cutoff = timezone.make_aware(datetime.combine(today - timedelta(days=settings.AUDIT_RETENTION_DAYS), day_time()))
    log_cutoff = today - timedelta(days=settings.LOG_RETENTION_DAYS)
    midnight = timezone.make_aware(datetime.combine(today, day_time()))

    # ponytail: re-export retained days for a small LAN installation; incremental
    # partition exports can replace this scan if measured audit volume requires it.
    upper_id = AuditEvent.objects.order_by("-id").values_list("id", flat=True).first() or 0
    rows = AuditEvent.objects.filter(pk__lte=upper_id).order_by("created_at", "id").values().iterator(chunk_size=500)
    for day, events in groupby(rows, key=lambda row: timezone.localdate(row["created_at"])):
        target = backups / f"audit-{day.isoformat()}.jsonl.gz"
        temp = target.with_name(target.name + f".{lease.owner}.tmp")
        count = 0
        try:
            with gzip.open(temp, "wt", encoding="utf-8") as output:
                for event in events:
                    lease.check()
                    output.write(json.dumps(event, cls=DjangoJSONEncoder, ensure_ascii=False) + "\n")
                    count += 1
            # Read back every record (including gzip CRC), rather than trust file existence.
            verified = 0
            with gzip.open(temp, "rt", encoding="utf-8") as archived:
                for line in archived:
                    lease.check()
                    json.loads(line)
                    verified += 1
            if count != verified:
                raise OSError("Audit backup verification failed")
            lease.check(force=True)
            os.replace(temp, target)
        finally:
            temp.unlink(missing_ok=True)

    closed_logs = []
    for source in logs.iterdir():
        match = FILE_PATTERN.fullmatch(source.name)
        if match and source.is_file() and not source.is_symlink():
            day = datetime.strptime(match[2], "%Y-%m-%d").date()
            if day < today:
                backup_log(source, backups / "logs" / source.name, lease)
                closed_logs.append((source, day))

    # Evaluation stderr is captured even before Django can initialize its handlers.
    evaluation_logs = Path(settings.BASE_DIR) / "logs" / "evaluation"
    for run in EvaluationRun.objects.filter(finished_at__lt=midnight).exclude(status__in=["queued", "running"]).iterator():
        source = evaluation_logs / f"{run.pk}.log"
        if source.is_file() and not source.is_symlink():
            destination = backups / "evaluation" / source.name
            backup_log(source, destination, lease)
            os.utime(destination, (run.finished_at.timestamp(), run.finished_at.timestamp()))
            closed_logs.append((source, timezone.localdate(run.finished_at)))

    # No cleanup is reached until ALL audit and log backups have succeeded.
    with transaction.atomic():
        lease.check(force=True)
        deleted, _ = AuditEvent.objects.filter(created_at__lt=audit_cutoff).delete()
    task_cutoff = timezone.make_aware(datetime.combine(log_cutoff, day_time())).timestamp()
    for job in IngestionJob.objects.filter(finished_at__lt=task_cutoff).exclude(status="running").iterator():
        if job.payload.get("logs"):
            lease.check()
            job.payload["logs"] = []
            job.save(update_fields=["payload"])
    for path, day in closed_logs:
        if day < log_cutoff:
            lease.check(force=True)
            path.unlink()
    for path in backups.iterdir():
        match = AUDIT_PATTERN.fullmatch(path.name)
        if match and not path.is_symlink() and datetime.strptime(match[1], "%Y-%m-%d").date() < timezone.localdate(audit_cutoff):
            lease.check(force=True)
            path.unlink()
    backup_logs = backups / "logs"
    if backup_logs.exists():
        for path in backup_logs.iterdir():
            match = FILE_PATTERN.fullmatch(path.name)
            if match and not path.is_symlink() and datetime.strptime(match[2], "%Y-%m-%d").date() < log_cutoff:
                lease.check(force=True)
                path.unlink()
    evaluation_backups = backups / "evaluation"
    if evaluation_backups.exists():
        for path in evaluation_backups.iterdir():
            if re.fullmatch(r"[a-f0-9-]{36}\.log", path.name) and path.is_file() and not path.is_symlink():
                if timezone.localdate(datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.get_current_timezone())) < log_cutoff:
                    lease.check(force=True)
                    path.unlink()
    return {"audit_deleted": deleted, "closed_log_files": len(closed_logs)}


def run_due(now=None):
    now = now or timezone.now()
    MaintenanceState.objects.get_or_create(name="daily")
    owner = uuid.uuid4().hex
    available = (Q(lease_until__isnull=True) | Q(lease_until__lte=now))
    due = Q(last_success__isnull=True) | Q(last_success__lt=scheduled_time(now))
    retry = Q(next_attempt__isnull=True) | Q(next_attempt__lte=now)
    if not MaintenanceState.objects.filter(available & due & retry, name="daily").update(
            owner=owner, lease_until=now + timedelta(minutes=5)):
        return False
    lease = Lease(owner)
    try:
        record_event("maintenance.started", "attempted", actor={"actor": "system"})
        summary = maintain(lease, now)
        lease.check(force=True)
        with transaction.atomic():
            record_event("maintenance.finished", "succeeded", actor={"actor": "system"}, details=summary)
            MaintenanceState.objects.filter(name="daily", owner=owner).update(
                last_success=now, next_attempt=None, lease_until=None, owner="", error="")
        return True
    except Exception as exc:
        logger.error("Daily maintenance failed (%s); cleanup requires a successful backup", type(exc).__name__)
        MaintenanceState.objects.filter(name="daily", owner=owner).update(
            next_attempt=timezone.now() + timedelta(minutes=10), lease_until=None, owner="",
            error=f"{type(exc).__name__}: 维护失败，请检查备份目录、磁盘空间和数据库；10分钟后重试")
        try:
            record_event("maintenance.finished", "failed", actor={"actor": "system"}, details={"error_type": type(exc).__name__})
        except Exception:
            pass
        return False


def _loop():
    while True:
        close_old_connections()
        try:
            run_due()
        except Exception as exc:
            # Migration may not have run yet. Retry without killing the web server.
            logger.error("Maintenance unavailable (%s); check migrations and database", type(exc).__name__)
        finally:
            close_old_connections()
        time.sleep(60)


def start_scheduler():
    global _started_pid
    if not settings.MAINTENANCE_ENABLED or "test" in sys.argv:
        return
    with _start_lock:
        if _started_pid == os.getpid():
            return
        thread = threading.Thread(target=_loop, name="daily-retention", daemon=True)
        thread.start()
        _started_pid = os.getpid()
