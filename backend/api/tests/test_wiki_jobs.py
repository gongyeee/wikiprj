import tempfile
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import RequestFactory, TestCase

from api import views
from api.models import IngestionJob


class WikiLayerJobHistoryTests(TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        IngestionJob.objects.all().delete()
        self.original_active_targets = views.ACTIVE_INGESTION_TARGETS
        views.ACTIVE_INGESTION_TARGETS = {}

    def tearDown(self):
        views.ACTIVE_INGESTION_TARGETS = self.original_active_targets
        self.temp_dir.cleanup()

    def write_job(self, job_id, started_at, status="succeeded"):
        payload = {
            "id": job_id,
            "status": status,
            "startedAt": started_at,
            "projectName": f"project-{job_id}",
            "projectDir": f"D:/projects/{job_id}",
            "logs": ["large log entry"],
            "command": ["python", "build.py"],
        }
        views.save_job(payload)

    def test_history_is_newest_first_and_excludes_large_detail_fields(self):
        self.write_job("older", 10)
        self.write_job("newer", 20, "running")

        jobs = views.list_ingestion_jobs()

        self.assertEqual([job["id"] for job in jobs], ["newer", "older"])
        self.assertNotIn("logs", jobs[0])
        self.assertNotIn("command", jobs[0])

    def test_history_limit(self):
        self.write_job("first", 10)
        self.write_job("second", 20)

        self.assertEqual(len(views.list_ingestion_jobs(limit=1)), 1)

    def test_upload_receipt_is_retained_and_invalid_counts_are_rejected(self):
        project = Path(self.temp_dir.name)
        receipt = {"chosen": 18, "uploaded": 17, "skipped": 1, "skippedFiles": ["unsupported.exe"]}
        with patch("api.views.threading.Thread"):
            job = views.start_ingestion_job({"uploadReceipt": receipt}, project)
            self.assertEqual(views.get_job(job["id"])["request"]["uploadReceipt"], receipt)
            with self.assertRaisesRegex(ValueError, "上传回执"):
                views.start_ingestion_job({"uploadReceipt": {**receipt, "uploaded": 18}}, project)

    def test_same_project_is_locked_while_different_projects_can_run(self):
        project_dir = Path(self.temp_dir.name) / "project-a"
        other_project_dir = Path(self.temp_dir.name) / "project-b"
        project_dir.mkdir()
        other_project_dir.mkdir()
        first_payload = {"projectDir": str(project_dir), "exportDir": str(project_dir / "exports" / "wiki")}
        second_payload = {"projectDir": str(other_project_dir), "exportDir": str(other_project_dir / "exports" / "wiki")}

        with patch("api.views.threading.Thread"):
            first = views.start_ingestion_job(first_payload, project_dir)
            with self.assertRaises(views.IngestionConflict) as raised:
                views.start_ingestion_job(first_payload, project_dir)
            second = views.start_ingestion_job(second_payload, other_project_dir)

        self.assertEqual(raised.exception.job_id, first["id"])
        self.assertNotEqual(first["exportDir"], second["exportDir"])

    def test_ingestion_job_uses_selected_registered_model_without_storing_key(self):
        project_dir = Path(self.temp_dir.name) / "selected-model-project"
        project_dir.mkdir()
        model = {
            "id": "model-a", "name": "抽取模型 A", "model": "model-a-name",
            "apiBase": "https://models.example/v1", "apiKey": "secret",
        }

        with patch("api.views.get_llm_model", return_value=model), patch("api.views.threading.Thread"):
            job = views.start_ingestion_job({"modelId": "model-a"}, project_dir)

        self.assertEqual(job["modelId"], "model-a")
        self.assertEqual(job["modelName"], "抽取模型 A")
        self.assertEqual(job["request"]["modelId"], "model-a")
        self.assertNotIn("apiKey", json.dumps(job, ensure_ascii=False))

    def test_rate_limit_marker_is_parsed(self):
        parsed = views.parse_rate_limit_error(
            'TASK_ERROR rate_limit {"message":"too many requests","retryAfterSeconds":8}'
        )
        self.assertEqual(parsed["message"], "too many requests")
        self.assertEqual(parsed["retryAfterSeconds"], 8)

    def test_failed_job_recovers_specific_reason_from_old_logs(self):
        message = "抽取输出达到 token 上限，未保存不完整结果"
        views.save_job({"id": "old-failure", "status": "failed", "error": "知识摄入进程失败",
                        "logs": ["RuntimeError: " + message]})
        saved = views.get_job("old-failure")
        self.assertEqual(saved["error"], message)
        self.assertEqual(saved["failureKind"], "output_limit")
        self.assertIsNone(views.parse_ingestion_failure('TASK_ERROR output_limit invalid'))

    def test_worker_preserves_structured_output_limit_error(self):
        project_dir = Path(self.temp_dir.name)
        job_id = "output-limit"
        views.save_job({"id": job_id, "status": "running", "logs": [], "command": ["unused"],
                        "projectDir": str(project_dir), "exportDir": str(project_dir / "export"),
                        "timeoutSeconds": 30})
        message = "模型耗尽 4096 token 输出预算，仅返回思考内容，没有生成抽取 JSON"
        lines = iter(['TASK_ERROR output_limit ' + json.dumps({"message": message}) + '\n', ''])
        process = Mock(stdout=SimpleNamespace(readline=lambda: next(lines), readlines=lambda: []))
        process.poll.side_effect = [None, 2]
        process.wait.return_value = 2
        with (patch.object(views, "get_llm_model", return_value={"model": "test"}),
              patch.object(views.subprocess, "Popen", return_value=process)):
            views.run_ingestion_job(job_id, project_dir / "ingest.py")
        saved = views.get_job(job_id)
        self.assertEqual(saved["status"], "failed")
        self.assertEqual(saved["error"], message)
        self.assertEqual(saved["failureKind"], "output_limit")
        self.assertTrue(saved["retryable"])

    def test_worker_marks_rate_limited_job_retryable_and_releases_project_lock(self):
        project_dir = Path(self.temp_dir.name) / "rate-limited-project"
        wiki_dir = project_dir / "wiki"
        project_dir.mkdir()
        job_id = "a" * 32
        job = {
            "id": job_id,
            "status": "running",
            "ok": None,
            "command": ["python", "build.py"],
            "timeoutSeconds": 30,
            "projectDir": str(project_dir),
            "exportDir": str(wiki_dir),
            "rawDir": str(project_dir / "raw" / "sources"),
            "logs": [],
            "progress": {"current": 0, "total": 2, "percent": 0},
        }
        views.save_job(job)
        for key in views.ingestion_target_keys(project_dir, wiki_dir):
            views.ACTIVE_INGESTION_TARGETS[key] = job_id

        class FakeStdout:
            def readline(self):
                return 'TASK_ERROR rate_limit {"message":"limited","retryAfterSeconds":5}\n'

            def readlines(self):
                return []

        class FakeProcess:
            stdout = FakeStdout()
            return_code = None

            def poll(self):
                return self.return_code

            def kill(self):
                self.return_code = -9

            def wait(self):
                return self.return_code

        script_path = Path(self.temp_dir.name) / "build.py"
        model = {
            "id": "model-a", "name": "抽取模型 A", "model": "model-a-name",
            "apiBase": "https://models.example/v1", "apiKey": "secret",
        }
        with (
            patch("api.views.get_llm_model", return_value=model),
            patch("api.views.subprocess.Popen", return_value=FakeProcess()) as popen,
        ):
            views.run_ingestion_job(job_id, script_path)

        saved = views.get_job(job_id)
        self.assertEqual(saved["status"], "rate_limited")
        self.assertTrue(saved["retryable"])
        self.assertEqual(saved["retryAfterSeconds"], 5)
        self.assertFalse(views.ACTIVE_INGESTION_TARGETS)
        self.assertEqual(popen.call_args.kwargs["env"]["LLM_MODEL"], "model-a-name")
        self.assertEqual(popen.call_args.kwargs["env"]["LLM_API_KEY"], "secret")

    def test_rate_limited_retry_reuses_parameters_and_halves_concurrency(self):
        project_dir = Path(self.temp_dir.name) / "retry-project"
        project_dir.mkdir()
        job_id = "b" * 32
        views.save_job({
            "id": job_id,
            "status": "rate_limited",
            "projectDir": str(project_dir),
            "logs": [],
            "attempt": 1,
            "request": {
                "projectDir": str(project_dir),
                "exportDir": str(project_dir / "exports" / "wiki"),
                "llmConcurrency": 8,
            },
        })
        request = RequestFactory().post(f"/api/ingestion/jobs/{job_id}/retry/")
        request.user = SimpleNamespace(is_authenticated=True, is_staff=True)

        with patch("api.views.start_ingestion_job", return_value={"id": "new-job"}) as start:
            response = views.retry_ingestion_job(request, job_id)

        self.assertEqual(response.status_code, 200)
        payload = start.call_args.args[0]
        self.assertEqual(payload["llmConcurrency"], 4)
        self.assertEqual(start.call_args.kwargs["retry_of"], job_id)
        self.assertEqual(start.call_args.kwargs["attempt"], 2)
        self.assertIsNone(start.call_args.kwargs["checkpoint_id"])

    def test_retry_reuses_existing_checkpoint(self):
        project_dir = Path(self.temp_dir.name) / "checkpoint-project"
        project_dir.mkdir()
        job_id = "d" * 32
        checkpoint_id = "e" * 32
        views.save_job({
            "id": job_id,
            "status": "failed",
            "projectDir": str(project_dir),
            "checkpointId": checkpoint_id,
            "logs": [],
            "attempt": 1,
            "request": {"projectDir": str(project_dir)},
        })
        request = RequestFactory().post(f"/api/ingestion/jobs/{job_id}/retry/")
        request.user = SimpleNamespace(is_authenticated=True, is_staff=True)

        with patch("api.views.start_ingestion_job", return_value={"id": "new-job"}) as start:
            response = views.retry_ingestion_job(request, job_id)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(start.call_args.kwargs["checkpoint_id"], checkpoint_id)

    def test_successful_worker_imports_payload_and_removes_staging_file(self):
        self.check_worker_publication(partial=False)

    def test_partial_worker_publishes_successes_and_keeps_retry_checkpoints(self):
        self.check_worker_publication(partial=True)

    def check_worker_publication(self, partial):
        project_dir = Path(self.temp_dir.name) / "direct-project"
        export_dir = project_dir / "exports" / "wiki"
        ingestion_output = project_dir / ".index" / "ingestion-test.json"
        ingestion_output.parent.mkdir(parents=True)
        report = {"failedDocuments": 1, "succeededDocuments": 1, "documents": [
            {"originalSource": "good.md", "status": "succeeded"}, {"originalSource": "bad.md", "status": "conversion_failed"}]} if partial else {}
        ingestion_output.write_text(json.dumps({"schemaVersion": 3, "objects": [], "ingestionReport": report}), encoding="utf-8")
        checkpoint_dir = project_dir / ".index" / "ingestion-checkpoints" / "checkpoint-test"
        checkpoint_dir.mkdir(parents=True)
        (checkpoint_dir / "manifest.json").write_text("{}", encoding="utf-8")
        job_id = "c" * 32
        views.save_job({
            "id": job_id,
            "status": "running",
            "command": ["python", "ingest.py"],
            "timeoutSeconds": 30,
            "projectDir": str(project_dir),
            "exportDir": str(export_dir),
            "ingestionOutput": str(ingestion_output),
            "checkpointDir": str(checkpoint_dir),
            "rawDir": str(project_dir / "raw"),
            "projectName": "direct-project",
            "logs": [],
            "progress": {"current": 1, "total": 1, "percent": 100},
        })

        class FakeStdout:
            def readline(self):
                return ""

            def readlines(self):
                return []

        class FakeProcess:
            stdout = FakeStdout()

            def poll(self):
                return 0

            def wait(self):
                return 0

        project = {"id": "project-id", "path": str(project_dir), "rawDir": str(project_dir / "raw"), "exportDir": str(export_dir)}
        with (
            patch("api.views.subprocess.Popen", return_value=FakeProcess()),
            patch("api.ingestion_staging.reconcile_publication"),
            patch("api.views.register_project_directory", return_value=project),
            patch("api.views.build_project_index", return_value={"ok": True, "knowledgeObjects": 0, "claims": 0}) as build,
            patch("api.views.export_project_markdown", return_value={"path": str(export_dir), "objects": 0, "sources": 0}),
        ):
            views.run_ingestion_job(job_id, Path(self.temp_dir.name) / "ingest.py")

        self.assertEqual(views.get_job(job_id)["status"], "partial_succeeded" if partial else "succeeded")
        self.assertEqual(views.get_job(job_id)["retryable"], partial)
        self.assertEqual(views.list_ingestion_jobs()[0]["ingestionReport"], report)
        self.assertEqual(build.call_args.kwargs["extracted_knowledge"]["schemaVersion"], 3)
        self.assertFalse(ingestion_output.exists())
        self.assertEqual(checkpoint_dir.exists(), partial)
