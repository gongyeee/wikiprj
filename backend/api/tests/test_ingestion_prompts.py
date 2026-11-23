import json
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from api import ingestion_prompts as prompts, views


class IngestionPromptTests(TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.path_patch = patch.object(prompts, "PROMPT_PATH", self.root / "prompts.json")
        self.path_patch.start()
        self.addCleanup(self.path_patch.stop)
        self.user = get_user_model().objects.create_user("prompt-editor", password="test-password", is_staff=True)
        self.client.force_login(self.user)

    def test_admin_editor_persists_and_validates_template_variables(self):
        response = self.client.get("/api/ingestion/prompts/")
        self.assertEqual(response.status_code, 200)
        values = response.json()["prompts"]
        before = prompts.prompt_fingerprint(values, "compact")
        values["compact"] += "\n自定义要求：保留条件。"
        response = self.client.post("/api/ingestion/prompts/", json.dumps({"prompts": values}), content_type="application/json")
        self.assertEqual(response.status_code, 200)
        self.assertNotEqual(before, prompts.prompt_fingerprint(prompts.load_prompts(), "compact"))
        values["compact"] = "没有原文占位符"
        rejected = self.client.post("/api/ingestion/prompts/", json.dumps({"prompts": values}), content_type="application/json")
        self.assertEqual(rejected.status_code, 400)
        self.assertIn("自定义要求", prompts.load_prompts()["compact"])
        self.user.is_staff = False
        self.user.save()
        self.assertEqual(self.client.get("/api/ingestion/prompts/").status_code, 403)

    def test_job_snapshots_prompt_and_retry_keeps_snapshot(self):
        project = self.root / "project"
        (project / "raw" / "sources").mkdir(parents=True)
        (project / "raw" / "sources" / "a.md").write_text("# 规则\n内容。", encoding="utf-8")
        job = retried = None
        try:
            with patch("api.views.threading.Thread"):
                job = views.start_ingestion_job({"extractionBatchChars": 2500, "extractionBatchSize": 4}, project)
                snapshot = Path(job["checkpointDir"]) / "prompts.json"
                original = prompts.load_prompts(snapshot)
                updated = {**original, "compact": original["compact"] + "\n新规则"}
                prompts.save_prompts(updated)
                views.release_ingestion_target(job["id"], job["exportDir"], job["projectDir"])
                retried = views.start_ingestion_job({}, project, checkpoint_id=job["checkpointId"])
            self.assertEqual(prompts.load_prompts(snapshot), original)
            self.assertEqual(job["request"]["extractionBatchChars"], 2500)
            self.assertIn("--prompt-file", retried["command"])
        finally:
            for value in (job, retried):
                if value:
                    views.release_ingestion_target(value["id"], value["exportDir"], value["projectDir"])
