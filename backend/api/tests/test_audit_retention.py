import gzip
import json
import logging
import re
import tempfile
import threading
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.db import OperationalError, close_old_connections, connection, transaction
from django.http import HttpResponse, JsonResponse, StreamingHttpResponse
from django.test import RequestFactory, TestCase, TransactionTestCase
from django.utils import timezone

from api import maintenance, views
from api.audit import AuditMiddleware, MUTATIONS, record_event
from api.urls import urlpatterns
from api.models import AuditEvent, IngestionJob, MaintenanceState, Project
from api.retained_logging import DailyFileHandler, log_secrets


class AuditTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("auditor", password="test-password")

    def test_login_denial_mutation_and_logout_without_secrets(self):
        response = self.client.post("/api/auth/login/", {"username": "auditor", "password": "private-wrong-password"}, content_type="application/json")
        self.assertEqual(response.status_code, 401)
        self.assertEqual(AuditEvent.objects.latest("id").result, "failed")
        self.assertEqual(self.client.post("/api/auth/login/", {"username": "auditor", "password": "test-password"}, content_type="application/json").status_code, 200)
        project = Project.objects.create(name="before", root_path="audit-project")
        response = self.client.patch("/api/projects/", {"id": str(project.id), "name": "after"}, content_type="application/json", HTTP_X_FORWARDED_FOR="8.8.8.8")
        self.assertEqual(response.status_code, 200)
        event = AuditEvent.objects.latest("id")
        self.assertEqual((event.action, event.target, event.actor_id, event.result), ("projects.patch", str(project.id), str(self.admin.pk), "succeeded"))
        self.assertEqual(event.source_ip, "127.0.0.1")
        self.client.post("/api/auth/logout/")
        self.assertEqual(AuditEvent.objects.latest("id").actor_id, str(self.admin.pk))
        self.assertEqual(self.client.get("/api/projects/").status_code, 401)
        self.assertEqual(AuditEvent.objects.latest("id").result, "denied")
        serialized = json.dumps(list(AuditEvent.objects.values("details")))
        self.assertNotIn("private-wrong-password", serialized)
        self.assertNotIn("test-password", serialized)
        self.assertNotIn("after", serialized)

    def test_audit_unavailable_prevents_mutation(self):
        self.client.force_login(self.admin)
        project = Project.objects.create(name="before", root_path="refused-project")
        with patch("api.audit.record_event", side_effect=OperationalError("unavailable")):
            response = self.client.patch("/api/projects/", {"id": str(project.id), "name": "after"}, content_type="application/json")
        self.assertEqual(response.status_code, 503)
        project.refresh_from_db()
        self.assertEqual(project.name, "before")

    def test_audit_errors_explain_schema_permissions_busy_and_disk_full(self):
        self.client.force_login(self.admin)
        for error, code in [
            ("no such table: api_auditevent", "audit_schema_not_ready"),
            ("database is locked", "audit_database_busy"),
            ("attempt to write a readonly database", "audit_database_not_writable"),
            ("unable to open database file", "audit_database_not_writable"),
            ("database or disk is full", "audit_storage_full"),
        ]:
            with self.subTest(code=code), patch("api.audit.record_event", side_effect=OperationalError(error)):
                response = self.client.post("/api/ingestion/build/", {}, content_type="application/json")
                self.assertEqual(response.status_code, 503)
                self.assertEqual(response.json()["code"], code)
                if code == "audit_schema_not_ready":
                    self.assertIn("manage.py migrate", response.json()["error"])
                if code == "audit_database_busy":
                    self.assertEqual(response["Retry-After"], "2")

    def test_admin_is_read_only_and_regular_users_cannot_read_audit(self):
        event = AuditEvent.objects.create(action="test", result="succeeded")
        self.client.force_login(self.admin)
        self.assertEqual(self.client.get("/admin/api/auditevent/").status_code, 200)
        self.assertEqual(self.client.post(f"/admin/api/auditevent/{event.pk}/delete/").status_code, 403)
        reader = get_user_model().objects.create_user("reader")
        self.client.force_login(reader)
        self.assertEqual(self.client.get("/admin/api/auditevent/").status_code, 302)

    def test_admin_login_records_actor_without_password(self):
        self.assertEqual(self.client.post("/admin/login/", {"username": "auditor", "password": "wrong-password"}).status_code, 200)
        self.assertEqual(AuditEvent.objects.latest("id").result, "failed")
        self.assertEqual(self.client.post("/admin/login/", {"username": "auditor", "password": "test-password", "next": "/admin/"}).status_code, 302)
        event = AuditEvent.objects.latest("id")
        self.assertEqual((event.action, event.actor_id, event.result), ("admin.login", str(self.admin.pk), "succeeded"))
        self.assertNotIn("test-password", json.dumps(event.details))

    def test_complete_task_log_exceeds_ui_tail_and_redacts_credentials(self):
        with tempfile.TemporaryDirectory() as directory, self.settings(RETAINED_LOG_DIR=Path(directory)):
            job = {"id": "task", "logs": []}
            token = log_secrets.set(("private-model-key",))
            try:
                for i in range(505):
                    views.append_job_log("task", f"line {i} private-model-key", job=job, persist=False)
            finally:
                log_secrets.reset(token)
            self.assertEqual(len(job["logs"]), 500)
            lines = next(Path(directory).glob("tasks-*.jsonl")).read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 505)
            self.assertEqual(json.loads(lines[0])["taskId"], "task")
            self.assertNotIn("private-model-key", "\n".join(lines))

    def test_daily_log_changes_date_without_shared_rotation(self):
        with tempfile.TemporaryDirectory() as directory, self.settings(RETAINED_LOG_DIR=Path(directory)):
            handler = DailyFileHandler()
            record = logging.LogRecord("test", logging.ERROR, "", 0, 'apiKey="private" Bearer another-secret', (), None)
            for day in (timezone.localdate(), timezone.localdate() + timedelta(days=1)):
                with patch("api.retained_logging.timezone.localdate", return_value=day):
                    handler.emit(record)
            files = list(Path(directory).glob("runtime-*.jsonl"))
            self.assertEqual(len(files), 2)
            self.assertNotIn("private", files[0].read_text())
            self.assertNotIn("another-secret", files[0].read_text())


class MaintenanceTests(TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.logs = Path(self.temp.name) / "logs"
        self.backups = Path(self.temp.name) / "backups"
        self.logs.mkdir()
        self.override = self.settings(RETAINED_LOG_DIR=self.logs, MAINTENANCE_BACKUP_DIR=self.backups,
                                      LOG_RETENTION_DAYS=90, AUDIT_RETENTION_DAYS=180)
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.now = timezone.now()

    def old_event(self, days):
        event = AuditEvent.objects.create(action="test", result="succeeded")
        AuditEvent.objects.filter(pk=event.pk).update(created_at=self.now - timedelta(days=days))
        return event

    def log_file(self, days):
        path = self.logs / f"tasks-{timezone.localdate(self.now - timedelta(days=days))}-123-abcdef01.jsonl"
        path.write_text('{"message":"retained"}\n', encoding="utf-8")
        return path

    def test_backup_verify_cleanup_and_once_per_scheduled_day(self):
        expired = self.old_event(181)
        kept = self.old_event(2)
        old_log = self.log_file(91)
        recent_log = self.log_file(2)
        current_log = self.log_file(0)
        unknown = self.logs / "keep-me.txt"
        unknown.write_text("unmanaged", encoding="utf-8")
        IngestionJob.objects.create(id="old-job", status="failed", started_at=0,
                                    finished_at=(self.now - timedelta(days=100)).timestamp(), payload={"logs": ["old"], "summary": "keep"})
        self.assertTrue(maintenance.run_due(self.now))
        self.assertFalse(AuditEvent.objects.filter(pk=expired.pk).exists())
        self.assertTrue(AuditEvent.objects.filter(pk=kept.pk).exists())
        self.assertFalse(old_log.exists())
        self.assertTrue(recent_log.exists())
        self.assertTrue(current_log.exists())
        self.assertTrue(unknown.exists())
        self.assertEqual((self.backups / "logs" / recent_log.name).read_bytes(), recent_log.read_bytes())
        self.assertFalse((self.backups / "logs" / current_log.name).exists())
        archive = next(self.backups.glob("audit-*.gz"))
        with gzip.open(archive, "rt", encoding="utf-8") as handle:
            self.assertEqual(json.loads(next(handle))["id"], kept.pk)
        self.assertEqual(IngestionJob.objects.get(pk="old-job").payload, {"logs": [], "summary": "keep"})
        with patch.object(maintenance, "maintain") as maintain:
            self.assertFalse(maintenance.run_due(self.now))
            maintain.assert_not_called()

    def test_backup_failure_preserves_expired_data_and_schedules_retry(self):
        expired = self.old_event(181)
        old_log = self.log_file(91)
        with patch.object(maintenance, "backup_log", side_effect=OSError("disk full")):
            self.assertFalse(maintenance.run_due(self.now))
        self.assertTrue(old_log.exists())
        self.assertTrue(AuditEvent.objects.filter(pk=expired.pk).exists())
        state = MaintenanceState.objects.get(pk="daily")
        self.assertIsNone(state.last_success)
        self.assertIsNotNone(state.next_attempt)
        self.assertTrue(state.error)
        self.assertFalse(maintenance.run_due(self.now))

    def test_catchup_lease_recovery_and_lease_loss(self):
        MaintenanceState.objects.create(name="daily", owner="other", lease_until=self.now + timedelta(minutes=2), last_success=self.now - timedelta(days=2))
        self.assertFalse(maintenance.run_due(self.now))
        MaintenanceState.objects.filter(pk="daily").update(lease_until=self.now - timedelta(seconds=1))
        self.assertTrue(maintenance.run_due(self.now))
        with self.assertRaises(RuntimeError):
            maintenance.Lease("other").check(force=True)

    def test_overlapping_directories_refuse_cleanup(self):
        old = self.log_file(91)
        with self.settings(MAINTENANCE_BACKUP_DIR=self.logs / "backup"):
            self.assertFalse(maintenance.run_due(self.now))
        self.assertTrue(old.exists())

    def test_schedule_before_two_am_targets_previous_day(self):
        early = timezone.localtime(self.now).replace(hour=1, minute=0)
        scheduled = timezone.localtime(maintenance.scheduled_time(early))
        self.assertEqual(scheduled.hour, 2)
        self.assertEqual(scheduled.date(), early.date() - timedelta(days=1))


class MaintenanceConcurrencyTests(TransactionTestCase):
    def test_only_one_worker_runs_maintenance_with_separate_connections(self):
        entered, release = threading.Event(), threading.Event()
        outcomes, errors = [], []

        def hold_maintenance(lease, now):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test timeout")
            lease.check(force=True)
            return {}

        def worker():
            close_old_connections()
            try:
                outcomes.append(maintenance.run_due())
            except Exception as exc:
                errors.append(exc)
            finally:
                close_old_connections()

        with patch.object(maintenance, "maintain", side_effect=hold_maintenance):
            thread = threading.Thread(target=worker)
            thread.start()
            try:
                self.assertTrue(entered.wait(5))
                self.assertFalse(maintenance.run_due())
            finally:
                release.set()
                thread.join(5)
        self.assertFalse(thread.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(outcomes, [True])


def audited_requests():
    """Exercise the real route table so new/renamed audited routes cannot disappear."""
    methods = {"projects": ("PATCH", "DELETE"), "llm_models": ("POST", "PUT", "DELETE")}
    for route in urlpatterns:
        name = route.callback.__name__
        if name not in MUTATIONS | {"login_view", "logout_view", "report", "draft_report"}:
            continue
        path = re.sub(r"<int:[^>]+>", "1", str(route.pattern))
        path = "/api/" + re.sub(r"<[^>]+>", "00000000-0000-0000-0000-000000000001", path)
        for method in methods.get(name, ("GET",) if name in {"report", "draft_report"} else ("POST",)):
            yield name, method, path


class AuditRouteContractTests(TestCase):
    def setUp(self):
        self.admin = get_user_model().objects.create_superuser("route-auditor", password="password")
        self.factory = RequestFactory()

    def request(self, method, path):
        request = self.factory.generic(method, path, b'{}', content_type="application/json")
        request.user = self.admin
        return request

    def test_all_audited_routes_preserve_success_errors_and_file_responses(self):
        checked = set()
        for name, method, path in audited_requests():
            checked.add(name)
            for status in (200, 400, 403, 404, 409, 500):
                with self.subTest(route=path, method=method, status=status):
                    payload = {"id": "task-123", "target": {"id": "project-123"}}
                    original = (HttpResponse(b"report", status=status, content_type="application/octet-stream")
                                if name in {"report", "draft_report"} else JsonResponse(payload, status=status))
                    expected = original.content
                    result = AuditMiddleware(lambda request: original)(self.request(method, path))
                    self.assertIs(result, original)
                    self.assertEqual((result.status_code, result.content), (status, expected))
                    event = AuditEvent.objects.latest("id")
                    self.assertEqual(event.actor_id, str(self.admin.pk))
                    self.assertEqual(event.result, "succeeded" if status < 400 else "denied" if status == 403 else "failed")
        self.assertTrue(MUTATIONS.issubset(checked))

    def test_search_and_streaming_answer_are_not_consumed_by_audit(self):
        def chunks():
            raise AssertionError("Audit must not consume the streaming answer")
            yield b""
        for name in ("search", "compose", "compose-stream"):
            original = StreamingHttpResponse(chunks()) if name.endswith("stream") else JsonResponse({"answer": "unchanged"})
            result = AuditMiddleware(lambda request: original)(self.request("POST", f"/api/projects/p/{name}/"))
            self.assertIs(result, original)
        self.assertEqual(AuditEvent.objects.count(), 0)


class AuditSchemaReadinessTests(TransactionTestCase):
    def test_real_missing_audit_table_then_upgrade_restores_every_audited_route(self):
        admin = get_user_model().objects.create_superuser("migration-auditor", password="password")
        factory = RequestFactory()
        called = []

        def respond(request):
            called.append(request.path)
            return JsonResponse({"ok": True})

        def request(method, path):
            req = factory.generic(method, path, b'{}', content_type="application/json")
            req.user = admin
            return req

        with connection.schema_editor() as editor:
            editor.delete_model(AuditEvent)
        try:
            # This is a real SQLite error, not a mocked exception.
            for name, method, path in audited_requests():
                response = AuditMiddleware(respond)(request(method, path))
                self.assertEqual(response.status_code, 503, path)
                self.assertEqual(json.loads(response.content)["code"], "audit_schema_not_ready", path)
            self.assertEqual(called, [])
            # A failed best-effort audit must not abort surrounding business transactions.
            with transaction.atomic():
                try:
                    record_event("test", "failed")
                except OperationalError:
                    pass
                Project.objects.create(name="preserved", root_path="migration-preserved")
        finally:
            with connection.schema_editor() as editor:
                editor.create_model(AuditEvent)
        for name, method, path in audited_requests():
            response = AuditMiddleware(respond)(request(method, path))
            self.assertEqual(response.status_code, 200, path)
        self.assertEqual(len(called), len(list(audited_requests())))
        self.assertTrue(Project.objects.filter(name="preserved").exists())
