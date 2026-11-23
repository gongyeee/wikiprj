import json
from unittest.mock import Mock, patch

import requests
from django.test import RequestFactory, SimpleTestCase, override_settings

from api import views


class SearchPerformanceTests(SimpleTestCase):
    model = {"apiBase": "http://model.test/v1", "apiKey": "secret", "model": "test"}

    def request(self, use_intent):
        return RequestFactory().post("/search/", json.dumps({
            "query": "app_shell文件格式", "useIntent": use_intent, "useSynonyms": False,
        }), content_type="application/json")

    def test_disabled_intent_search_never_calls_model_and_reports_stage_timings(self):
        with (
            patch.object(views, "get_project", return_value={"id": "p"}),
            patch.object(views, "get_llm_model", return_value=self.model),
            patch.object(views, "analyze_query_intent", side_effect=AssertionError("unexpected model call")),
            patch.object(views, "project_search", return_value={"results": []}) as search,
            patch.object(views, "store_search_context", return_value="context"),
            patch("builtins.print") as log,
        ):
            response = views.search(self.request(False), "p")
        self.assertEqual(response.status_code, 200)
        performance = json.loads(response.content)["performance"]
        self.assertEqual(set(performance["timingsMs"]),
                         {"project_lookup", "query_enrichment", "local_retrieval", "search_context"})
        self.assertEqual(performance["intentModelMs"], 0)
        search.assert_called_once()
        self.assertNotIn("app_shell", str(log.call_args_list))
        self.assertIn("SEARCH_PERF", str(log.call_args_list))

    @override_settings(QUERY_INTENT_TIMEOUT=7, QUERY_INTENT_MAX_TOKENS=512)
    def test_intent_timeout_falls_back_and_local_search_still_runs(self):
        with (
            patch.object(views, "get_project", return_value={"id": "p"}),
            patch.object(views, "get_llm_model", return_value=self.model),
            patch("api.llm.requests.post", side_effect=requests.Timeout("test timeout")) as model,
            patch.object(views, "project_search", return_value={"results": []}) as search,
            patch.object(views, "store_search_context", return_value="context"),
            patch("builtins.print"),
        ):
            response = views.search(self.request(True), "p")
        self.assertEqual(response.status_code, 200)
        data = json.loads(response.content)
        self.assertFalse(data["queryIntent"]["enabled"])
        self.assertIn("意图识别失败", data["queryIntent"]["reason"])
        self.assertIn("modelCallElapsedMs", data["queryIntent"])
        self.assertEqual(model.call_args.kwargs["timeout"], (5, 7))
        self.assertEqual(model.call_args.kwargs["json"]["max_tokens"], 512)
        search.assert_called_once()

    def test_truncated_intent_is_rejected_and_response_closed(self):
        from api.llm import analyze_query_intent
        response = Mock()
        response.json.return_value = {"choices": [{"finish_reason": "length", "message": {"content": "{}"}}]}
        with patch("api.llm.requests.post", return_value=response):
            result = analyze_query_intent("app_shell文件格式", self.model)
        self.assertFalse(result["enabled"])
        self.assertIn("token 上限", result["reason"])
        response.close.assert_called_once()

    def test_missing_project_is_rejected_before_model_enrichment(self):
        with (
            patch.object(views, "get_project", return_value=None),
            patch.object(views, "build_query_enrichment", side_effect=AssertionError("unexpected enrichment")),
            patch("builtins.print"),
        ):
            self.assertEqual(views.search(self.request(True), "missing").status_code, 404)
