import io
import json
import tempfile
import uuid
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import OperationalError
from django.test import Client, TestCase
from django.utils import timezone
from openpyxl import Workbook, load_workbook

from api.evaluation_data import (commit_draft, import_preview, match_references, question_key, transfer_preview)
from api.evaluation_excel import read_upload, report_bytes, template_bytes
from api.evaluation_runner import (create_run, expire_stale_runs, launch_run, run_config, run_worker, summary_for, validate_score)
from api.models import EvaluationCase, EvaluationResult, EvaluationRun, Project
from api.projects import get_project, register_project_directory
from api.search_index import build_project_index


class EvaluationTests(TestCase):
    def test_database_errors_are_not_misreported_as_update_conflicts(self):
        for message, status, code in [
            ("no such table: api_evaluationrun", 503, "database_schema_not_ready"),
            ("database is locked", 503, "database_busy"),
            ("unable to open database file", 500, "database_operation_failed"),
        ]:
            with self.subTest(code=code), patch("api.evaluation_views.expire_stale_runs", side_effect=OperationalError(message)), self.assertLogs("api.evaluation_views", level="ERROR"):
                response = self.client.get("/api/evaluation/config/")
            self.assertEqual(response.status_code, status)
            self.assertEqual(response.json()["code"], code)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.override = self.settings(DOMAIN_LAYER_DIR=Path(self.temp.name) / "layers")
        self.override.enable()
        self.addCleanup(self.override.disable)
        self.admin = get_user_model().objects.create_user("eval-admin", password="test", is_staff=True)
        self.user = get_user_model().objects.create_user("eval-user", password="test")
        self.client.force_login(self.admin)
        root = Path(self.temp.name) / "one"
        raw = root / "raw" / "sources"
        raw.mkdir(parents=True)
        (raw / "rules.md").write_text("# 保护限价\n买入申报价格不得高于保护限价。\n", encoding="utf-8")
        self.project = Project.objects.get(pk=register_project_directory(root, "项目一")["id"])
        self.other = Project.objects.create(name="项目二", root_path=str(Path(self.temp.name) / "two"))
        self.base = f"/api/evaluation/projects/{self.project.pk}"

    def post(self, path, body):
        return self.client.post(path, data=json.dumps(body), content_type="application/json")

    def make_case(self, question="保护限价有什么要求？", project=None, **data):
        payload = {"question": question, "expected": "买入申报价格不得高于保护限价。", "status": "enabled", "kind": "answer", "category": "业务", "scene": "交易规则", **data}
        return EvaluationCase.objects.create(project=project or self.project, question_key=question_key(question), data=payload, created_by="admin", updated_by="admin")

    def fake_config(self):
        with patch("api.evaluation_runner.list_llm_models", return_value=[{"id":"default"}]), patch("api.evaluation_runner.get_llm_model", return_value={"id":"default", "model":"test-model", "name":"测试模型", "apiBase":"http://internal.test/v1", "apiKey":"secret"}):
            return run_config({"useIntent": False, "useSynonyms": False})

    def make_run(self, cases=None):
        with patch("api.evaluation_runner.launch_run"):
            return create_run(self.admin, self.project, [(c.id, {**c.data, "case_revision": c.revision}) for c in (cases or [self.make_case()])], self.fake_config())

    def score(self, **overrides):
        return {"correctness": 45, "completeness": 27, "support": 18, "critical_error": False, "reason":"关键条件正确", "missing_points":[], "wrong_points":[], **overrides}

    def test_every_evaluation_endpoint_rejects_non_admin(self):
        random_id = uuid.uuid4()
        endpoints = [("get", "/api/evaluation/config/"), ("get", "/api/evaluation/template/"),
                     ("get", self.base + "/cases/"), ("post", self.base + "/cases/"),
                     ("post", self.base + "/bulk/"), ("post", self.base + "/imports/"),
                     ("post", self.base + "/transfer/"), ("get", self.base + "/runs/"), ("post", self.base + "/runs/"),
                     ("post", f"/api/evaluation/drafts/{random_id}/commit/"), ("get", f"/api/evaluation/drafts/{random_id}/report/"),
                     ("get", f"/api/evaluation/runs/{random_id}/"), ("post", f"/api/evaluation/runs/{random_id}/"),
                     ("get", f"/api/evaluation/runs/{random_id}/report/"), ("post", f"/api/evaluation/runs/{random_id}/results/1/review/")]
        for user in (None, self.user):
            self.client.logout()
            if user:
                self.client.force_login(user)
            for method, url in endpoints:
                with self.subTest(user=user, url=url, method=method):
                    self.assertEqual(getattr(self.client, method)(url).status_code, 403 if user else 401)

    def test_csrf_and_project_boundary_and_revision(self):
        case = self.make_case()
        secure = Client(enforce_csrf_checks=True)
        secure.force_login(self.admin)
        self.assertEqual(secure.post(self.base + "/cases/", data="{}", content_type="application/json").status_code, 403)
        token = secure.get("/api/evaluation/config/").json()["csrfToken"]
        self.assertEqual(secure.post(self.base + "/cases/", data=json.dumps({"question":"新问题", "expected":"新答案"}), content_type="application/json", HTTP_X_CSRFTOKEN=token).status_code, 200)
        other_case = self.make_case(project=self.other)
        res = self.post(self.base + "/bulk/", {"ids":[str(other_case.id)], "action":"delete", "confirmed_count":1})
        self.assertEqual(res.status_code, 400)
        self.assertTrue(EvaluationCase.objects.filter(pk=other_case.id).exists())
        res = self.post(self.base + "/cases/", {**case.data, "id":str(case.id), "revision":0})
        self.assertEqual(res.status_code, 409)
        self.assertEqual(self.client.get(self.base + "/cases/?q=保护限价").json()["total"], 1)

    def test_import_validation_duplicates_idempotency_and_stale_preview(self):
        old = self.make_case()
        preview = import_preview(self.admin, self.project.pk, [old.data, {"question":"新问题", "expected":"多行\n答案"}, {"question":"缺答案"}, {"question":"新问题", "expected":"重复"}])
        self.assertEqual(preview["counts"], {"create":1, "update":0, "skip":1, "error":2})
        commit_draft(self.admin, preview["draft_id"])
        commit_draft(self.admin, preview["draft_id"])
        self.assertEqual(EvaluationCase.objects.filter(project=self.project).count(), 2)
        preview = import_preview(self.admin, self.project.pk, [{**old.data, "expected":"更新答案"}], "update")
        old.revision += 1
        old.save()
        with self.assertRaisesRegex(ValueError, "已变更"):
            commit_draft(self.admin, preview["draft_id"])
        self.assertNotEqual(EvaluationCase.objects.get(pk=old.pk).data["expected"], "更新答案")

    def test_import_multiple_projects_and_ambiguous_names_and_expiry(self):
        preview = import_preview(self.admin, self.project.pk, [{"project":self.other.name, "question":"目标题", "expected":"答"}, {"project":"未知项目", "question":"未知题", "expected":"答"}])
        self.assertEqual(preview["counts"]["create"], 1)
        self.assertEqual(preview["counts"]["error"], 1)
        commit_draft(self.admin, preview["draft_id"])
        self.assertEqual(EvaluationCase.objects.get(data__question="目标题").project_id, self.other.pk)
        from api.models import EvaluationDraft
        preview = import_preview(self.admin, self.project.pk, [{"question":"过期题", "expected":"答"}])
        EvaluationDraft.objects.filter(pk=preview["draft_id"]).update(expires_at=timezone.now()-timedelta(seconds=1))
        with self.assertRaisesRegex(ValueError, "过期"):
            commit_draft(self.admin, preview["draft_id"])

    def test_transfer_conflicts_reference_revalidation_and_snapshot_survival(self):
        case = self.make_case(reference_paths="rules.md")
        conflict = self.make_case("重复题")
        self.make_case("重复题", project=self.other)
        run = self.make_run([case])
        preview = transfer_preview(self.admin, self.project.pk, {"ids":[str(case.pk),str(conflict.pk)], "action":"move", "target":str(self.other.pk)})
        self.assertEqual(preview["counts"]["skip"], 1)
        self.assertEqual(preview["counts"]["pending"], 1)
        commit_draft(self.admin, preview["draft_id"])
        case.refresh_from_db()
        self.assertEqual(case.project_id, self.other.pk)
        self.assertEqual(case.data["status"], "pending")
        self.assertEqual(run.project_id, self.project.pk)
        self.assertEqual(run.results.get().snapshot["expected"], "买入申报价格不得高于保护限价。")
        preview = transfer_preview(self.admin, self.other.pk, {"ids":[str(case.pk)], "action":"copy", "target":str(self.project.pk)})
        commit_draft(self.admin, preview["draft_id"])
        self.assertTrue(EvaluationCase.objects.filter(pk=case.pk, project=self.other).exists())

    def test_excel_merged_headers_newlines_formula_and_template(self):
        wb = Workbook()
        ws = wb.active
        ws.title = "人工问答"
        ws.append(["问题分类", "问题", "预期答案"])
        ws.append(["竞价", "第一题", "第一点\n第二点"])
        ws.append([None, "第二题", "普通答案"])
        ws.append([None, "公式题", "=1+1"])
        ws.merge_cells("A2:A3")
        buf = io.BytesIO(); wb.save(buf)
        def upload():
            return SimpleUploadedFile("gold.xlsx", buf.getvalue())
        meta = read_upload(upload(), "人工问答")
        self.assertEqual(meta["mapping"]["question"], 2)
        rows = read_upload(upload(), "人工问答", mapping=meta["mapping"])
        self.assertEqual(rows[1]["category"], "竞价")
        self.assertEqual(rows[0]["expected"], "第一点\n第二点")
        preview = import_preview(self.admin, self.project.pk, rows)
        self.assertEqual(preview["counts"]["error"], 1)
        self.assertIn("公式", preview["rows"][2]["error"])
        template = load_workbook(io.BytesIO(template_bytes()))
        self.assertEqual(template["标准问答"].cell(1, 4).value, "问题")
        self.assertNotIn("产品", [cell.value for cell in template["标准问答"][1]])

    def test_scoring_schema_refusal_critical_error_and_no_evidence(self):
        snapshot = {"kind":"answer"}
        score = validate_score(self.score(critical_error=True), snapshot, 80, True)
        self.assertEqual(score["total"], 90)
        self.assertFalse(score["passed"])
        self.assertEqual(validate_score(self.score(), snapshot, 80, False)["support"], 0)
        for value in (True, -1, 51, float("nan")):
            with self.assertRaises(ValueError):
                validate_score(self.score(correctness=value), snapshot, 80, True)
        score = validate_score(self.score(refusal_correct=True), {"kind":"refusal"}, 80, False)
        self.assertEqual(score["total"], 100)
        self.assertTrue(score["passed"])

    def test_review_preserves_auto_score_conflict_and_full_answer_report(self):
        run = self.make_run()
        result = run.results.get()
        result.answer = "=危险公式也只是文本\n" + "实际回答不得高于保护限价。" * 3000
        result.status = "scored"
        result.score = validate_score(self.score(), result.snapshot, 80, True)
        result.evidence = {"context":"[1] rules.md\n买入申报价格不得高于保护限价。"}
        result.save()
        url = f"/api/evaluation/runs/{run.id}/results/{result.pk}/review/"
        body = {"status":"adjusted", "score":70, "passed":False, "comment":"遗漏了适用条件", "revision":0}
        self.assertEqual(self.post(url, body).status_code, 200)
        self.assertEqual(self.post(url, body).status_code, 409)
        result.refresh_from_db()
        self.assertEqual(result.score["total"], 90)
        self.assertEqual(result.review["score"], 70)
        self.assertEqual(len(result.review["history"]), 1)
        summary = summary_for(run, [result])
        wb = load_workbook(io.BytesIO(report_bytes(run, [result], summary)))
        actual = "".join(row[4] or "" for row in wb["答案全文对照"].iter_rows(min_row=2, values_only=True))
        self.assertEqual(actual, result.answer)
        self.assertTrue(all(cell.data_type != "f" for ws in wb for row in ws for cell in row))
        self.assertEqual(summary["human_average"], 70)
        self.assertEqual(summary["review_coverage"], 1)
        exported = self.client.get(f"/api/evaluation/runs/{run.pk}/report/")
        self.assertEqual(exported.status_code, 200)
        self.assertIn("attachment", exported["Content-Disposition"])
        self.assertEqual(load_workbook(io.BytesIO(exported.content))["逐题审查"].cell(2, 12).value, 70)

    def test_stale_worker_is_interrupted_and_pending_filter_includes_empty_review(self):
        run = self.make_run()
        EvaluationRun.objects.filter(pk=run.pk).update(heartbeat_at=timezone.now()-timedelta(minutes=3))
        expire_stale_runs()
        run.refresh_from_db()
        self.assertEqual(run.status, "interrupted")
        self.assertEqual(run.results.get().status, "interrupted")
        response = self.client.get(f"/api/evaluation/runs/{run.pk}/?review=pending").json()
        self.assertEqual(response["total"], 1)

    def test_worker_uses_question_only_and_reports_model_failure(self):
        case = self.make_case(expected="不应提供给回答模型的标准答案")
        run = self.make_run([case])
        model = {"id":"default", "model":"test-model", "name":"测试模型", "apiBase":"http://internal.test/v1", "apiKey":"secret"}
        events = [{"event":"chunk", "content":"实际回答"}, {"event":"done", "answer":"实际回答", "llmConfigured":True, "context":"[1] 实际证据"}]
        prepared = {"llmDocuments":[{"path":"rules.md", "content":"实际证据"}], "contextOptions":{}, "documents":[]}
        def judge(*args, **kwargs):
            saved = run.results.get()
            self.assertEqual(saved.answer, "实际回答")
            self.assertEqual(saved.status, "running")
            self.assertEqual(saved.metrics["stage"], "score")
            return self.score()
        with patch("api.evaluation_runner.get_llm_model", return_value=model), patch("api.evaluation_runner.prepare_answer", return_value=(prepared, [])) as prepare, patch("api.evaluation_runner.stream_answer_with_llm", return_value=iter(events)) as answer, patch("api.evaluation_runner.chat_json", side_effect=judge):
            run_worker(run.pk)
        result = run.results.get()
        self.assertEqual(result.status, "scored")
        self.assertNotIn(case.data["expected"], str(prepare.call_args))
        self.assertNotIn(case.data["expected"], str(answer.call_args))
        self.assertNotIn("apiKey", json.dumps(run.config))
        run2 = self.make_run([case])
        events = [{"event":"done", "answer":"降级文本", "llmConfigured":False, "llmError":"unavailable"}]
        with patch("api.evaluation_runner.get_llm_model", return_value=model), patch("api.evaluation_runner.prepare_answer", return_value=(prepared, [])), patch("api.evaluation_runner.stream_answer_with_llm", return_value=iter(events)), patch("api.evaluation_runner.chat_json") as judge:
            run_worker(run2.pk)
        self.assertEqual(run2.results.get().status, "answer_failed")
        judge.assert_not_called()

    def test_launcher_captures_stderr_and_marks_start_failure(self):
        run = self.make_run()
        def start(*args, **kwargs):
            kwargs["stderr"].write(b"worker startup diagnostic\n")
            self.assertIs(kwargs["stdout"], kwargs["stderr"])
            raise OSError("cannot start")
        with self.settings(BASE_DIR=Path(self.temp.name)), patch("api.evaluation_runner.subprocess.Popen", side_effect=start):
            launch_run(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "interrupted")
        self.assertEqual(run.results.get().status, "interrupted")
        self.assertIn("worker startup diagnostic", (Path(self.temp.name) / "logs" / "evaluation" / f"{run.pk}.log").read_text())

    def test_real_pipeline_collects_index_evidence_without_gold_answer(self):
        from api.evaluation_runner import prepare_answer
        prepared, results = prepare_answer(self.project.pk, "保护限价", {"useIntent":False, "useSynonyms":False, "limit":8})
        self.assertTrue(results)
        self.assertTrue(prepared["llmDocuments"])
        self.assertIn("不得高于", prepared["llmDocuments"][0]["content"])

    def test_score_failure_keeps_actual_answer_and_retry_includes_manual_rerun(self):
        case = self.make_case()
        run = self.make_run([case])
        model = {"id":"default", "model":"test-model", "name":"测试模型", "apiBase":"http://internal.test/v1", "apiKey":"secret"}
        events = [{"event":"done", "answer":"已生成的实际答案", "llmConfigured":True, "context":"原文证据"}]
        prepared = {"llmDocuments":[], "contextOptions":{}, "documents":[]}
        with patch("api.evaluation_runner.get_llm_model", return_value=model), patch("api.evaluation_runner.prepare_answer", return_value=(prepared, [])), patch("api.evaluation_runner.stream_answer_with_llm", return_value=iter(events)), patch("api.evaluation_runner.chat_json", return_value={"total":100}):
            run_worker(run.pk)
        result = run.results.get()
        self.assertEqual(result.status, "score_failed")
        self.assertEqual(result.answer, "已生成的实际答案")
        result.status = "scored"
        result.review = {"status":"rerun", "comment":"重新检查"}
        result.save()
        with patch("api.evaluation_views.run_config", return_value=self.fake_config()):
            res = self.post(f"/api/evaluation/runs/{run.pk}/", {"action":"retry"})
        self.assertEqual(res.status_code, 201)
        retried = EvaluationRun.objects.get(pk=res.json()["run"]["id"])
        self.assertEqual(retried.config["retry_of"], str(run.pk))
        self.assertEqual(retried.results.count(), 1)
        self.assertEqual(run.results.get().review["status"], "rerun")

    def test_cancel_during_answer_keeps_answer_and_stops_judging_and_next_case(self):
        run = self.make_run([self.make_case(), self.make_case("第二题")])
        model = {"id":"default", "model":"test-model", "name":"测试模型", "apiBase":"http://internal.test/v1", "apiKey":"secret"}
        def stream(*args):
            EvaluationRun.objects.filter(pk=run.pk).update(cancel_requested=True)
            yield {"event":"done", "answer":"取消时仍保留实际答案", "llmConfigured":True, "context":"原文"}
        prepared = {"llmDocuments":[], "contextOptions":{}, "documents":[]}
        with patch("api.evaluation_runner.get_llm_model", return_value=model), patch("api.evaluation_runner.prepare_answer", return_value=(prepared, [])), patch("api.evaluation_runner.stream_answer_with_llm", side_effect=stream), patch("api.evaluation_runner.chat_json") as judge:
            run_worker(run.pk)
        run.refresh_from_db()
        self.assertEqual(run.status, "cancelled")
        self.assertEqual(list(run.results.values_list("status", flat=True)), ["cancelled", "cancelled"])
        self.assertEqual(run.results.first().answer, "取消时仍保留实际答案")
        judge.assert_not_called()

    def test_cancel_releases_project_and_discards_late_model_results(self):
        model = {"id":"default", "model":"test-model", "name":"测试模型", "apiBase":"http://internal.test/v1", "apiKey":"secret"}
        prepared = {"llmDocuments":[], "contextOptions":{}, "documents":[]}
        for stage in ("answer", "score"):
            with self.subTest(stage=stage):
                case = self.make_case(question=f"取消测试 {stage}")
                run = self.make_run([case])
                def cancel():
                    response = self.post(f"/api/evaluation/runs/{run.pk}/", {"action":"cancel"})
                    self.assertEqual(response.status_code, 200)
                    run.refresh_from_db()
                    self.assertEqual(run.status, "cancelled")
                    self.assertEqual(run.results.get().status, "cancelled")
                    replacement = self.make_run([case])
                    self.assertEqual(replacement.status, "queued")
                    self.post(f"/api/evaluation/runs/{replacement.pk}/", {"action":"cancel"})
                def stream(*args):
                    if stage == "answer":
                        cancel()
                    yield {"event":"done", "answer":"模型答案", "llmConfigured":True, "context":"证据"}
                def judge(*args, **kwargs):
                    cancel()
                    return self.score()
                with patch("api.evaluation_runner.get_llm_model", return_value=model), patch("api.evaluation_runner.prepare_answer", return_value=(prepared, [])), patch("api.evaluation_runner.stream_answer_with_llm", side_effect=stream), patch("api.evaluation_runner.chat_json", side_effect=judge):
                    run_worker(run.pk)
                run.refresh_from_db()
                result = run.results.get()
                self.assertEqual(run.status, "cancelled")
                self.assertEqual(result.status, "cancelled")
                self.assertEqual(result.score, {})
                self.assertEqual(result.answer, "模型答案" if stage == "score" else "")

    def test_grouped_chunk_selection_and_empty_search_do_not_trigger_second_search(self):
        from api.evaluation_runner import prepare_answer
        with patch("api.evaluation_runner.project_search", return_value={"results":[]}) as search, patch("api.views.project_search") as repeated:
            prepared, results = prepare_answer(self.project.pk, "不存在的术语", {"useIntent":False,"useSynonyms":False})
            self.assertEqual(results, [])
            self.assertEqual(prepared["llmDocuments"], [])
            search.assert_called_once()
            repeated.assert_not_called()
        with patch("api.evaluation_runner.project_search", return_value={"results":[{"path":"rules.md","chunkId":1,"chunkIds":[1,2]}]}), patch("api.views.prepare_compose", return_value={}) as compose:
            prepare_answer(self.project.pk, "保护限价", {"useIntent":False,"useSynonyms":False})
            self.assertEqual(compose.call_args.args[1]["chunkIds"], [1,2])

    def test_empty_model_response_is_explicit_failure_not_successful_fallback(self):
        from api.llm import answer_with_llm, stream_answer_with_llm
        from unittest.mock import Mock
        model = {"apiBase":"http://test/v1", "apiKey":"key", "model":"test"}
        response = Mock()
        response.json.return_value = {"choices":[{"message":{"content":""}}]}
        response.iter_lines.return_value = iter([b'data: [DONE]'])
        with patch("api.llm.requests.post", return_value=response):
            answer = answer_with_llm("问题", [{"path":"x", "content":"原文"}], model)
            self.assertFalse(answer["llmConfigured"])
            self.assertIn("llmError", answer)
            events = list(stream_answer_with_llm("问题", [{"path":"x", "content":"原文"}], model))
            self.assertFalse(events[-1]["llmConfigured"])
            self.assertIn("llmError", events[-1])
