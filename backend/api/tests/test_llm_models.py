import tempfile
from pathlib import Path

from django.test import SimpleTestCase

from api import llm_models


class LlmModelRegistryTests(SimpleTestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.original_registry_path = llm_models.REGISTRY_PATH
        llm_models.REGISTRY_PATH = Path(self.temp_dir.name) / "llm_models.json"

    def tearDown(self):
        llm_models.REGISTRY_PATH = self.original_registry_path
        self.temp_dir.cleanup()

    def test_create_read_update_and_delete_model(self):
        created = llm_models.create_llm_model({
            "id": "custom-model",
            "name": "初始名称",
            "model": "provider/model-v1",
            "apiBase": "https://llm.example/v1",
            "apiKey": "secret-key",
        })
        self.assertEqual(created["id"], "custom-model")
        self.assertTrue(created["hasApiKey"])
        self.assertEqual(llm_models.get_llm_model("custom-model")["apiKey"], "secret-key")

        updated = llm_models.update_llm_model({
            "id": "custom-model",
            "name": "更新后的名称",
            "model": "provider/model-v2",
            "apiBase": "https://llm.example/v2",
            "apiKey": "",
        })
        self.assertEqual(updated["name"], "更新后的名称")
        self.assertEqual(updated["model"], "provider/model-v2")
        self.assertEqual(llm_models.get_llm_model("custom-model")["apiKey"], "secret-key")

        cleared = llm_models.update_llm_model({
            "id": "custom-model",
            "name": "更新后的名称",
            "model": "provider/model-v2",
            "apiBase": "https://llm.example/v2",
            "clearApiKey": True,
        })
        self.assertFalse(cleared["hasApiKey"])

        remaining = llm_models.delete_llm_model("custom-model")
        self.assertFalse(any(item["id"] == "custom-model" for item in remaining))

    def test_duplicate_id_is_rejected(self):
        payload = {"id": "same-id", "model": "provider/model", "apiBase": "https://llm.example/v1"}
        llm_models.create_llm_model(payload)
        with self.assertRaisesRegex(ValueError, "已存在"):
            llm_models.create_llm_model(payload)

    def test_default_model_cannot_be_changed(self):
        with self.assertRaisesRegex(ValueError, "不能修改"):
            llm_models.update_llm_model({"id": "default", "model": "other"})
