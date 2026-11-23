import os
import subprocess
import tempfile
import zipfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from api import document_conversion as conversion, search_index as index
from api.ingestion_staging import prepare_markdown, stage_prepared_import, finish_staging, reconcile_publication


class IngestionResultTests(SimpleTestCase):
    def test_published_versions_reconcile_to_one_logical_document_and_missing_file_is_explicit(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            raw.mkdir()
            for name in ("v1.md", "v2.md"):
                (raw / name).write_text(f"# 规则\n{name} 有效正文", encoding="utf-8")
            project = {"path": directory, "rawDir": str(raw)}
            items, prepared, report = prepare_markdown(project, root / "artifacts")
            staged, baseline = stage_prepared_import(project, root / "stage.db", items, prepared, report)
            finish_staging(staged, report, set())
            self.assertEqual(report["phase"], "ready_to_publish")
            self.assertNotIn("publishedDocuments", report)
            assignments = [{"sourceFile": name, "documentName": "规则", "versionLabel": str(i), "setDefault": i == 2}
                           for i, name in enumerate(("v1.md", "v2.md"), 1)]
            self.assertTrue(index.publish_document_import(project, root / "stage.db", baseline, {"objects": []},
                                                         source_assignments=assignments)["ok"])
            reconcile_publication(project, report)
            self.assertEqual((report["totalDocuments"], report["publishedDocuments"], report["projectDocuments"], report["projectVersions"]), (2, 2, 1, 2))
            self.assertEqual(sum(row["isDefaultVersion"] for row in report["documents"]), 1)
            report["documents"].append({"sourceRef": "missing.md", "status": "succeeded"})
            reconcile_publication(project, report)
            self.assertEqual(report["publicationFailedDocuments"], 1)
            self.assertEqual(report["totalDocuments"], report["succeededDocuments"] + report["failedDocuments"] + report["pendingDocuments"])

    def test_no_chunks_cannot_be_counted_as_extraction_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw"
            raw.mkdir()
            (raw / "empty.md").write_text("# 标题\n测试正文", encoding="utf-8")
            project = {"path": directory, "rawDir": str(raw)}
            items, prepared, report = prepare_markdown(project, root / "artifacts")
            with patch.object(index, "split_document_chunks", return_value=[]):
                staged, _ = stage_prepared_import(project, root / "stage.db", items, prepared, report)
            finish_staging(staged, report, set())
            self.assertEqual((report["succeededDocuments"], report["indexFailedDocuments"]), (0, 1))
            self.assertIn("正文切片", report["documents"][0]["error"])

    def test_doc_missing_dependency_is_actionable(self):
        with (patch.dict(os.environ, {"LIBREOFFICE_PATH": "Z:/missing/soffice.exe"}),
              self.assertRaisesRegex(RuntimeError, "DOC.*LibreOffice.*LIBREOFFICE_PATH")):
            conversion._convert_legacy_word(Path("rules.doc"))

    def test_doc_bridge_passes_converted_word_to_markitdown(self):
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "soffice.exe"
            executable.touch()
            source = Path(directory) / "rules.doc"
            source.write_bytes(b"legacy Word input")

            def convert(args, **kwargs):
                target = Path(args[args.index("--outdir") + 1]) / "rules.docx"
                with zipfile.ZipFile(target, "w") as archive:
                    archive.writestr("word/document.xml", '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Legacy Word body</w:t></w:r></w:p></w:body></w:document>')
                return subprocess.CompletedProcess(args, 0)

            real_run = subprocess.run
            def run(args, **kwargs):
                return convert(args, **kwargs) if args[0] == str(executable) else real_run(args, **kwargs)

            with (patch.dict(os.environ, {"LIBREOFFICE_PATH": str(executable)}),
                  patch.object(conversion.subprocess, "run", side_effect=run)):
                self.assertIn("Legacy Word body", conversion.convert_to_markdown(source))
