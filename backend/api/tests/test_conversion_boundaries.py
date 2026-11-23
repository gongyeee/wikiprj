import json
import os
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from api import document_conversion as conversion
from api.file_paths import fs_path
from api.ingestion_prompts import DEFAULT_PROMPTS, load_prompts, save_prompts
from tools.ingest_project import checkpoint_file, load_section_checkpoint, save_section_checkpoint


class ConversionBoundaryTests(SimpleTestCase):
    def test_long_checkpoint_and_prompt_snapshot_paths_round_trip(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            checkpoint = root / ("long-project-" + "x" * 115) / "ingestion-checkpoints" / ("a" * 32)
            key = "b" * 64
            target = checkpoint_file(checkpoint, key)
            self.assertGreater(len(str(target)), 260)
            result = {"summary": "已完成", "objects": [], "relations": []}
            try:
                save_section_checkpoint(checkpoint, key, "规则.md", {"sectionId": "s", "chunkId": 1}, result)
                self.assertEqual(load_section_checkpoint(checkpoint, key), result)
                snapshot = checkpoint / "prompts.json"
                save_prompts(DEFAULT_PROMPTS, snapshot)
                self.assertEqual(load_prompts(snapshot), DEFAULT_PROMPTS)
                self.assertFalse(list(fs_path(checkpoint).glob("*.tmp")))
                self.assertFalse(list(target.parent.glob("*.tmp")))
            finally:
                # Remove only files created inside this test's temporary directory;
                # keep extended Windows paths for files exceeding MAX_PATH.
                for path in fs_path(root).rglob("*"):
                    if path.is_file():
                        path.unlink()

    def test_known_extension_does_not_initialize_magika(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "说明.txt"
            path.write_text("中文正文", encoding="utf-8")
            with patch("magika.Magika", side_effect=AssertionError("must not initialize format detector")):
                self.assertEqual(conversion._convert_direct(path), "中文正文")

    def test_binary_conversion_timeout_is_explicit_and_names_the_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "慢文件.pdf"
            path.write_bytes(b"dummy")
            with (patch.dict(os.environ, {"DOCUMENT_CONVERSION_TIMEOUT": "7"}),
                  patch.object(conversion.subprocess, "run", side_effect=subprocess.TimeoutExpired("convert", 7)) as run):
                with self.assertRaisesRegex(ValueError, "慢文件.pdf.*7 秒.*已终止"):
                    conversion.convert_to_markdown(path)
            self.assertEqual(run.call_args.kwargs["timeout"], 7)
            self.assertIn("convert_document.py", run.call_args.args[0][1])

    def test_real_worker_is_killed_when_conversion_hangs(self):
        original_run = subprocess.run
        process_id = []
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "hang.pdf"
            path.write_bytes(b"dummy")

            def stalled_worker(args, **kwargs):
                script = "import os,time; print(os.getpid(), flush=True); time.sleep(30)"
                try:
                    return original_run([args[0], "-u", "-c", script], timeout=.2, capture_output=True, check=False)
                except subprocess.TimeoutExpired as exc:
                    process_id.append(int(exc.output.strip()))
                    raise

            with patch.object(conversion.subprocess, "run", side_effect=stalled_worker):
                with self.assertRaisesRegex(ValueError, "转换超时"):
                    conversion.convert_to_markdown(path)
            self.assertEqual(len(process_id), 1)
