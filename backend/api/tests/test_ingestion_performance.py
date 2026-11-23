import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from django.test import SimpleTestCase, TestCase

from api import views
from tools import ingest_project as ingest


class ExtractionTransportTests(SimpleTestCase):
    def test_gateway_reasoning_alias_is_counted_without_becoming_answer(self):
        from api.llm import reasoning_text
        self.assertEqual(reasoning_text({"reasoning": "内部思考"}), "内部思考")
        self.assertEqual(reasoning_text({"reasoning_content": "规范字段", "reasoning": "重复字段"}), "规范字段")
        response = self.response([
            {"choices": [{"delta": {"reasoning": "内部思考"}}]},
            {"choices": [{"delta": {"content": "{}"}, "finish_reason": "stop"}]},
        ])
        session = Mock()
        session.post.return_value = response
        with (
            patch.object(ingest, "http_session", return_value=session),
            patch.object(ingest, "LLM_STREAM", True),
            patch.object(ingest, "perf_event") as event,
        ):
            self.assertEqual(ingest.request_chat_text({"messages": []}), "{}")
        metrics = event.call_args.kwargs
        self.assertEqual(metrics["reasoningChars"], 4)
        self.assertEqual(metrics["outputChars"], 2)
        self.assertEqual(metrics["finishReason"], "stop")
        self.assertEqual(metrics.get("emptyDeltaEvents", 0), 0)
        self.assertNotIn("内部思考", json.dumps(event.call_args_list, ensure_ascii=False))

    def test_gateway_reasoning_alias_reports_reasoning_only_output_limit(self):
        response = self.response([{"choices": [{"delta": {"reasoning": "内部思考"}, "finish_reason": "length"}]}])
        session = Mock()
        session.post.return_value = response
        with (
            patch.object(ingest, "http_session", return_value=session),
            patch.object(ingest, "LLM_STREAM", True),
            patch.object(ingest, "perf_event") as event,
        ):
            with self.assertRaisesRegex(ingest.ExtractionOutputLimitError, "仅返回思考内容"):
                ingest.request_chat_text({"messages": []})
        self.assertEqual(event.call_args.kwargs["finishReason"], "length")

    def test_extraction_thinking_policy_preserves_other_models_and_explicit_settings(self):
        cases = [
            ({"model": "deepseek-v4-flash"}, "auto", {"type": "disabled"}),
            ({"model": "provider/deepseek-v4-pro"}, "auto", {"type": "disabled"}),
            ({"model": "other-model"}, "auto", None),
            ({"model": "deepseek-v4-flash", "thinking": {"type": "enabled"}}, "auto", {"type": "enabled"}),
            ({"model": "deepseek-v4-flash", "reasoning_effort": "high"}, "auto", None),
            ({"model": "other-model"}, "enabled", {"type": "enabled"}),
        ]
        for payload, mode, expected in cases:
            with (self.subTest(payload=payload, mode=mode),
                  patch.object(ingest, "INGEST_THINKING", mode),
                  patch.object(ingest, "LLM_STREAM", False),
                  patch.object(ingest, "chat_text_blocking", return_value="{}") as request,
                  patch.object(ingest, "perf_event")):
                ingest.request_chat_text(payload)
                sent = request.call_args.args[0]
                self.assertEqual(sent.get("thinking"), expected)
                self.assertEqual(sent["max_tokens"], ingest.LLM_MAX_TOKENS)
                if "reasoning_effort" in payload:
                    self.assertEqual(sent["reasoning_effort"], "high")

    def test_reasoning_only_limit_is_specific_and_does_not_retry_or_repair(self):
        response = self.response([{
            "choices": [{"delta": {"reasoning_content": "思考内容"}, "finish_reason": "length"}],
            "usage": {"completion_tokens": 4096},
        }])
        session = Mock()
        session.post.return_value = response
        with (patch.object(ingest, "http_session", return_value=session),
              patch.object(ingest, "LLM_STREAM", True),
              patch.object(ingest, "LLM_MAX_TOKENS", 4096),
              patch.object(ingest, "perf_event") as event):
            with self.assertRaisesRegex(ingest.ExtractionOutputLimitError, "4096.*仅返回思考内容"):
                ingest.chat_json("extract")
        session.post.assert_called_once()
        response.close.assert_called_once()
        self.assertEqual(event.call_args.kwargs["outputChars"], 0)
        self.assertEqual(event.call_args.kwargs["reasoningChars"], 4)

    def response(self, events):
        data = "".join("data: " + json.dumps(event, ensure_ascii=False) + "\n\n" for event in events)
        data += "data: [DONE]\n\n"
        encoded = data.encode("utf-8")
        response = Mock(status_code=200)
        # Split UTF-8 characters and SSE lines across transport chunks.
        response.iter_content.return_value = (encoded[i:i + 7] for i in range(0, len(encoded), 7))
        return response

    def test_stream_decodes_utf8_reuses_session_and_closes_response(self):
        response = self.response([{"choices": [{"delta": {"content": "中文原文"}}]}])
        session = Mock()
        session.post.return_value = response
        with patch.object(ingest, "http_session", return_value=session):
            self.assertEqual(ingest.chat_text_stream({}), "中文原文")
        self.assertEqual(session.post.call_args.kwargs["headers"]["Accept-Encoding"], "identity")
        response.close.assert_called_once()
        with patch.object(ingest, "_HTTP_LOCAL", SimpleNamespace()), patch.object(ingest.requests, "Session") as factory:
            self.assertIs(ingest.http_session(), ingest.http_session())
            factory.assert_called_once()

    def test_length_limit_and_continuous_reasoning_fail_without_json_repair(self):
        for events, times, error in [
            ([{"choices": [{"delta": {"content": "partial"}, "finish_reason": "length"}]}], [0, 1], RuntimeError),
            ([{"choices": [{"delta": {"reasoning_content": "still thinking"}}]}], [0, 181], TimeoutError),
        ]:
            response = self.response(events)
            session = Mock()
            session.post.return_value = response
            with (
                self.subTest(error=error),
                patch.object(ingest, "http_session", return_value=session),
                patch.object(ingest.time, "monotonic", side_effect=times),
                patch.object(ingest, "LLM_REQUEST_TIMEOUT", 180),
                patch.object(ingest, "LLM_STREAM", True),
            ):
                with self.assertRaises(error):
                    ingest.chat_json("extract")
                self.assertEqual(session.post.call_count, 1)
                self.assertIn("max_tokens", session.post.call_args.kwargs["json"])
                response.close.assert_called_once()

    def test_trailing_commas_repaired_locally_without_changing_strings(self):
        text = '{"summary":"literal ,} and ,]","objects":[{"name":"订单",},],"relations":[],}'
        with patch.object(ingest, "request_chat_text", return_value=text) as request:
            result = ingest.chat_json("extract")
        self.assertEqual(result["summary"], "literal ,} and ,]")
        self.assertEqual(result["objects"], [{"name": "订单"}])
        request.assert_called_once()
        with self.assertRaises(json.JSONDecodeError):
            ingest.parse_extraction_json('{"objects":[{"name":"未完成')

    def test_prompt_profiles_separate_checkpoints(self):
        section = {"sectionId": "a", "contentHash": "b"}
        with patch.object(ingest, "PROMPT_PROFILE", "standard"):
            standard = ingest.section_checkpoint_key("source.md", section)
        with patch.object(ingest, "PROMPT_PROFILE", "compact"):
            self.assertNotEqual(standard, ingest.section_checkpoint_key("source.md", section))

    def test_request_metrics_distinguish_first_event_reasoning_and_content(self):
        response = self.response([
            {"choices": [{"delta": {"reasoning_content": "思考"}}]},
            {"choices": [{"delta": {"content": "正文"}}]},
            {"choices": [], "usage": {"completion_tokens": 6}},
        ])
        session = Mock()
        session.post.return_value = response
        with (
            patch.object(ingest, "http_session", return_value=session),
            patch.object(ingest, "LLM_STREAM", True),
            patch.object(ingest.time, "monotonic", side_effect=[0, 1, 3, 4, 5, 5]),
            patch.object(ingest, "perf_event") as event,
        ):
            self.assertEqual(ingest.request_chat_text({"messages": []}), "正文")
        metric = event.call_args.kwargs
        self.assertEqual(event.call_args.args, ("llm_request",))
        self.assertEqual(metric["firstEventSeconds"], 1)
        self.assertEqual(metric["firstContentSeconds"], 3)
        self.assertEqual(metric["reasoningChars"], 2)
        self.assertEqual(metric["outputChars"], 2)
        self.assertEqual(metric["completionTokens"], 6)
        self.assertEqual(metric["requestMaxTokens"], ingest.LLM_MAX_TOKENS)
        self.assertTrue(metric["requestStream"])
        self.assertNotIn("思考", json.dumps(metric, ensure_ascii=False))
        self.assertNotIn("正文", json.dumps(metric, ensure_ascii=False))

    def test_unrecognized_stream_records_bounded_shapes_without_content(self):
        events = [{"data": {"answer": "保密正文"}, "sequence": index} for index in range(20)]
        response = self.response(events)
        session = Mock()
        session.post.return_value = response
        with (
            patch.object(ingest, "http_session", return_value=session),
            patch.object(ingest, "LLM_STREAM", True),
            patch.object(ingest, "perf_event") as event,
        ):
            with self.assertRaisesRegex(RuntimeError, "流式返回为空"):
                ingest.request_chat_text({"messages": []})
        metric = event.call_args.kwargs
        self.assertEqual(metric["emptyChoiceEvents"], 20)
        self.assertEqual(len(metric["eventShapes"]), 1)
        self.assertEqual(metric["eventShapes"][0]["dataKeys"], ["answer"])
        self.assertNotIn("保密正文", json.dumps(event.call_args_list, ensure_ascii=False))
        self.assertEqual(sum(call.args[0] == "llm_event_shape" for call in event.call_args_list), 1)

    def test_error_stream_fails_immediately_instead_of_waiting_for_timeout(self):
        response = self.response([{"error": {"code": "backend_error", "message": "保密错误详情"}}])
        session = Mock()
        session.post.return_value = response
        with (
            patch.object(ingest, "http_session", return_value=session),
            patch.object(ingest, "LLM_STREAM", True),
            patch.object(ingest, "perf_event") as event,
        ):
            with self.assertRaisesRegex(RuntimeError, "error 事件"):
                ingest.request_chat_text({"messages": []})
        metric = event.call_args.kwargs
        self.assertEqual(metric["serverErrorEvents"], 1)
        self.assertEqual(metric["events"], 1)
        self.assertFalse(metric["ok"])
        self.assertNotIn("保密错误详情", json.dumps(event.call_args_list, ensure_ascii=False))
        response.close.assert_called_once()


class JobLogBatchTests(TestCase):
    def test_worker_batches_logs_and_flushes_final_progress_on_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            job_id = "log-batch"
            views.save_job({
                "id": job_id, "status": "running", "command": ["unused"],
                "projectDir": directory, "exportDir": str(Path(directory) / "export"),
                "timeoutSeconds": 30, "logs": [],
            })
            perf_lines = [
                'CONVERT {"stage":"batch_start","total":2,"concurrency":2,"converterVersion":"test"}\n',
                'CONVERT {"stage":"file_done","total":2,"current":1,"sourceRef":"a.md","cached":true}\n',
                'CONVERT {"stage":"file_failed","total":2,"current":2,"sourceRef":"b.md"}\n',
                'PERF {"stage":"llm_request","ok":true}\n',
                'PERF {"stage":"json_repair"}\n',
                'PERF {"stage":"llm_request","ok":false}\n',
            ]
            lines = iter(perf_lines + [f"PROGRESS chunks {i} 1000\n" for i in range(1, 1001)] + [""])
            stdout = SimpleNamespace(readline=lambda: next(lines), readlines=lambda: [])
            process = Mock(stdout=stdout)
            process.poll.side_effect = [None] * 1006 + [1]
            process.wait.return_value = 1
            with (
                patch.object(views, "get_llm_model", return_value={"model": "test"}),
                patch.object(views.subprocess, "Popen", return_value=process),
                patch.object(views.time, "monotonic", return_value=1),
                patch.object(views, "save_job", wraps=views.save_job) as save,
            ):
                views.run_ingestion_job(job_id, Path(directory) / "ingest.py")
                self.assertEqual(save.call_count, 1)
            job = views.get_job(job_id)
            self.assertEqual(job["status"], "failed")
            self.assertEqual(job["progress"]["current"], 1000)
            self.assertEqual(len(job["logs"]), 500)
            self.assertEqual(job["performance"]["receivedLines"], 1006)
            self.assertEqual(job["performance"]["conversion"]["current"], 2)
            self.assertEqual(job["performance"]["conversion"]["failed"], 1)
            self.assertEqual(job["performance"]["conversion"]["cacheHits"], 1)
            self.assertEqual(job["performance"]["modelRequests"], 2)
            self.assertEqual(job["performance"]["modelErrors"], 1)
            self.assertEqual(job["performance"]["jsonRepairRequests"], 1)
            records = [json.loads(line) for line in Path(job["performanceLog"]).read_text(encoding="utf-8").splitlines()]
            self.assertEqual([record["stage"] for record in records],
                             ["parent_start", "conversion_batch_start", "conversion_file_done", "conversion_file_failed",
                              "llm_request", "json_repair", "llm_request", "parent_summary"])
            self.assertEqual(records[-1]["status"], "failed")

    def test_periodic_log_write_timer_captures_slow_database_save(self):
        with tempfile.TemporaryDirectory() as directory:
            job_id = "slow-log"
            views.save_job({"id": job_id, "status": "running", "command": ["unused"],
                            "projectDir": directory, "exportDir": str(Path(directory) / "export"),
                            "timeoutSeconds": 30, "logs": []})
            stdout = SimpleNamespace(readline=lambda: "PROGRESS chunks 1 2\n", readlines=lambda: [])
            process = Mock(stdout=stdout)
            process.poll.return_value = 1
            process.wait.return_value = 1
            with (
                patch.object(views, "get_llm_model", return_value={"model": "test"}),
                patch.object(views.subprocess, "Popen", return_value=process),
                patch.object(views.time, "monotonic", side_effect=[0, 2, 2, 2]),
                patch.object(views.time, "perf_counter", side_effect=[0, 1, 1.25, 5]),
            ):
                views.run_ingestion_job(job_id, Path(directory) / "ingest.py")
            metrics = views.get_job(job_id)["performance"]
            self.assertEqual(metrics["periodicLogSaveCount"], 1)
            self.assertEqual(metrics["periodicLogSaveSeconds"], .25)
            self.assertEqual(metrics["periodicLogSaveMaxMs"], 250)
            self.assertEqual(metrics["periodicLogSaveWallPercent"], 5)
