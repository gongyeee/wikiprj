import json
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from api import search_index as index
from api.ingestion_staging import prepare_markdown, stage_prepared_import, finish_staging
from tools import ingest_project as ingest


class PartialIngestionTests(SimpleTestCase):
    def test_conversion_first_partial_publication_and_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw" / "sources"
            raw.mkdir(parents=True)
            for name, text in {"good.md": "# 好文档\n正常内容", "extract.md": "# 好块\n完整块\n\n# 坏块\n故障块",
                               "broken.docx": "not a zip"}.items():
                (raw / name).write_text(text, encoding="utf-8")
            output, checkpoint = root / "payload.json", root / "checkpoints" / "run1"
            argv = ["ingest", "--project-dir", directory, "--ingestion-output", str(output),
                    "--checkpoint-dir", str(checkpoint), "--extraction-batch-size", "1"]
            original_convert = index.convert_to_markdown
            converted = []

            def convert(path):
                converted.append(path.suffix)
                if path.suffix == ".docx":
                    raise ValueError("损坏的文件")
                return original_convert(path)

            def extract(source, text, *args):
                # Every conversion has finished before the first LLM call.
                self.assertEqual(len(converted), 3)
                if "故障块" in text:
                    raise TimeoutError("单文档抽取失败")
                return {"summary": "有效摘要", "objects": [], "relations": []}

            with (patch.object(sys, "argv", argv), patch.object(ingest, "LLM_API_KEY", "test"),
                  patch.object(ingest, "INGEST_RETRIES", 0), patch.object(index, "convert_to_markdown", side_effect=convert),
                  patch.object(ingest, "extract_section_with_llm", side_effect=extract)):
                ingest.main()
            payload = json.loads(output.read_text(encoding="utf-8"))
            report = payload["ingestionReport"]
            self.assertEqual((report["succeededDocuments"], report["failedDocuments"]), (1, 2))
            self.assertEqual({row["status"] for row in report["documents"]}, {"succeeded", "conversion_failed", "extraction_failed"})
            self.assertEqual(len(list(Path(report["artifactDir"]).glob("markdown/*.md"))), 2)
            good = next(row for row in report["documents"] if row["sourceRef"] == "good.md")
            self.assertEqual(Path(good["markdownPath"]).read_text(encoding="utf-8"), "# 好文档\n\n正常内容")
            project = {"path": directory, "rawDir": str(raw)}
            self.assertIsNone(index.connect_index(project))
            self.assertTrue(index.publish_document_import(project, output.with_suffix(".db"), payload["documentImport"]["baseline"], payload)["ok"])
            connection = index.connect_index(project)
            self.assertEqual([row[0] for row in connection.execute("SELECT relative_path FROM documents")], ["good.md"])
            self.assertTrue(index.verify_project_index(project)["ok"])
            connection.close()
            # Successful docs use both cached Markdown and extraction on retry.
            converted.clear()
            with (patch.object(sys, "argv", argv), patch.object(ingest, "LLM_API_KEY", "test"),
                  patch.object(index, "convert_to_markdown", side_effect=convert),
                  patch.object(ingest, "extract_section_with_llm", return_value={"summary": "补齐", "objects": [], "relations": []}) as request):
                ingest.main()
            self.assertEqual(converted, [".docx"])
            self.assertFalse(any(call.args[0] == "good.md" for call in request.call_args_list))

    def test_failed_existing_document_removed_and_unrelated_document_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            raw.mkdir()
            for name in ("good.md", "bad.md", "unrelated.md"):
                (raw / name).write_text(f"# {name}\n正文 {name}", encoding="utf-8")
            project = {"path": directory, "rawDir": str(raw)}
            self.assertTrue(index.build_project_index(project)["ok"])
            before = index.get_ingestion_documents(project, ["unrelated.md"])
            items, prepared, report = prepare_markdown(project, root / ".index" / "artifacts", ["good.md", "bad.md"])
            staged, baseline = stage_prepared_import(project, root / "stage.db", items, prepared, report)
            finish_staging(staged, report, {"bad.md"})
            self.assertEqual(len(index.get_ingestion_documents(project)), 3)
            self.assertTrue(index.publish_document_import(project, root / "stage.db", baseline, {"objects": []})["ok"])
            self.assertEqual(index.get_ingestion_documents(project, ["unrelated.md"]), before)
            self.assertEqual({doc["sourceRef"] for doc in index.get_ingestion_documents(project)}, {"good.md", "unrelated.md"})
