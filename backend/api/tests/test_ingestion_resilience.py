import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest.mock import Mock, patch

from django.test import SimpleTestCase

from tools import ingest_project as ingest


class IngestionResilienceTests(SimpleTestCase):
    def setUp(self):
        ingest._CANCELLED.clear()

    def tearDown(self):
        ingest._CANCELLED.clear()

    def entries(self):
        return [(index, {"chunkId": index + 1, "sectionId": f"s{index}", "text": f"原文{index}",
                         "headingPath": f"标题{index}", "contentHash": f"h{index}"}, f"key{index}", f"块{index}") for index in range(3)]

    def test_batch_keeps_result_ids_and_falls_back_on_missing_ids(self):
        entries = self.entries()
        rows = [{"chunkId": i + 1, "summary": str(i), "objects": [], "relations": []} for i in range(3)]
        with tempfile.TemporaryDirectory() as directory, patch.object(ingest, "LLM_API_KEY", "test"):
            with patch.object(ingest, "chat_json", return_value={"results": list(reversed(rows))}) as request:
                result = ingest.extract_group(Path(directory), "a.md", entries, False)
            self.assertEqual([row[1]["summary"] for row in result["successes"]], ["0", "1", "2"])
            self.assertFalse(result["failures"])
            request.assert_called_once()
            with (
                patch.object(ingest, "chat_json", return_value={"results": rows[:1]}),
                patch.object(ingest, "extract_section_with_llm", return_value={"summary": "单块", "objects": [], "relations": []}) as single,
            ):
                fallback = ingest.extract_group(Path(directory), "a.md", entries, False)
            self.assertEqual(len(fallback["successes"]), 3)
            self.assertEqual(single.call_count, 3)

    def test_batch_budgets_and_transient_retry_are_bounded(self):
        groups = list(ingest.group_sections(self.entries() * 5, 3000, 6))
        self.assertEqual([len(group) for group in groups], [6, 6, 3])
        action = Mock(side_effect=[TimeoutError("temporary"), "ok"])
        with patch.object(ingest, "INGEST_RETRIES", 1), patch.object(ingest._CANCELLED, "wait", return_value=False):
            self.assertEqual(ingest.retry_extraction(action, "test"), "ok")
        self.assertEqual(action.call_count, 2)

    def test_failed_chunk_does_not_stop_siblings_and_retry_reuses_successes(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw" / "sources"
            raw.mkdir(parents=True)
            (raw / "规则.md").write_text("# 好节\n正确内容。\n\n# 坏节\n错误内容。\n\n# 末节\n末尾内容。", encoding="utf-8")
            output, checkpoint = root / "output.json", root / "checkpoints"
            argv = ["ingest", "--project-dir", directory, "--ingestion-output", str(output),
                    "--checkpoint-dir", str(checkpoint), "--extraction-batch-size", "1", "--llm-concurrency", "2"]

            def extract(source, text, *args):
                if "错误" in text:
                    raise TimeoutError("test failure")
                return {"summary": text, "objects": [], "relations": []}

            with (patch.object(sys, "argv", argv), patch.object(ingest, "LLM_API_KEY", "test"),
                  patch.object(ingest, "INGEST_RETRIES", 0), patch.object(ingest, "extract_section_with_llm", side_effect=extract)):
                with self.assertRaises(ingest.IncompleteIngestionError):
                    ingest.main()
            self.assertFalse(output.exists())
            self.assertEqual(len(list((checkpoint / "sections").glob("*.json"))), 2)
            self.assertEqual(len(json.loads((checkpoint / "failures.json").read_text(encoding="utf-8"))["failures"]), 1)
            with (patch.object(sys, "argv", argv), patch.object(ingest, "LLM_API_KEY", "test"),
                  patch.object(ingest, "extract_section_with_llm", return_value={"summary": "补齐", "objects": [], "relations": []}) as single):
                ingest.main()
            single.assert_called_once()
            self.assertIn("错误", single.call_args.args[1])
            self.assertTrue(output.exists())
            self.assertEqual(json.loads((checkpoint / "failures.json").read_text(encoding="utf-8"))["failures"], [])

    def test_watchdog_interrupts_silent_body_and_global_cancel_closes_stream(self):
        for cancel in (False, True):
            with self.subTest(cancel=cancel):
                ingest._CANCELLED.clear()
                release, registered = threading.Event(), threading.Event()

                class Handler(BaseHTTPRequestHandler):
                    def do_POST(self):
                        self.rfile.read(int(self.headers.get("Content-Length", "0")))
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        # Continuous reasoning keeps the socket read timeout alive.
                        event = b'data: {"choices":[{"delta":{"reasoning":"thinking"}}]}\n\n'
                        until = time.monotonic() + 4
                        while not release.wait(.02) and time.monotonic() < until:
                            try:
                                self.wfile.write(event)
                                self.wfile.flush()
                            except OSError:
                                break

                    def log_message(self, *args):
                        pass

                server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
                server.daemon_threads = True
                server_thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
                server_thread.start()
                original = ingest.register_response

                def register(response):
                    original(response)
                    registered.set()

                try:
                    with (patch.object(ingest, "LLM_API_BASE", f"http://127.0.0.1:{server.server_port}"),
                          patch.object(ingest, "LLM_STREAM", True), patch.object(ingest, "FIRST_CONTENT_TIMEOUT", .2 if not cancel else 3),
                          patch.object(ingest, "register_response", side_effect=register), ThreadPoolExecutor(max_workers=1) as executor):
                        started = time.perf_counter()
                        future = executor.submit(ingest.request_chat_text, {"messages": []})
                        self.assertTrue(registered.wait(2))
                        if cancel:
                            ingest.cancel_active_requests()
                        with self.assertRaises(ingest.IngestionCancelled if cancel else ingest.FirstContentTimeout):
                            future.result(timeout=2)
                        self.assertLess(time.perf_counter() - started, 2)
                        self.assertFalse(ingest._ACTIVE_RESPONSES)
                finally:
                    release.set()
                    server.shutdown()
                    server.server_close()
                    server_thread.join(2)

    def test_fatal_cli_exit_does_not_join_worker_waiting_for_headers(self):
        release, both_started = threading.Event(), threading.Event()
        counter_lock = threading.Lock()
        count = 0

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                nonlocal count
                self.rfile.read(int(self.headers.get("Content-Length", "0")))
                with counter_lock:
                    count += 1
                    number = count
                if number == 1:
                    both_started.wait(2)
                    try:
                        self.send_error(401)
                    except OSError:
                        pass
                else:
                    both_started.set()
                    release.wait(5)

            def log_message(self, *args):
                pass

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        server.daemon_threads = True
        thread = threading.Thread(target=lambda: server.serve_forever(poll_interval=.01), daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                raw = root / "raw" / "sources"
                raw.mkdir(parents=True)
                for name in ("a.md", "b.md"):
                    (raw / name).write_text("# 规则\n规则内容。", encoding="utf-8")
                env = {**os.environ, "LLM_API_BASE": f"http://127.0.0.1:{server.server_port}",
                       "LLM_API_KEY": "test", "INGEST_RETRIES": "0", "LLM_STREAM": "1", "PYTHONDONTWRITEBYTECODE": "1"}
                started = time.perf_counter()
                result = subprocess.run([sys.executable, str(Path(ingest.__file__)), "--project-dir", directory,
                                         "--ingestion-output", str(root / "output.json"), "--extraction-batch-size", "1",
                                         "--llm-concurrency", "2"], env=env, capture_output=True, text=True,
                                        encoding="utf-8", timeout=4)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("TASK_ERROR fatal", result.stdout)
                self.assertTrue(both_started.is_set())
                self.assertLess(time.perf_counter() - started, 4)
                self.assertFalse((root / "output.json").exists())
        finally:
            release.set()
            server.shutdown()
            server.server_close()
            thread.join(2)
