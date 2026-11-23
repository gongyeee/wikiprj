from types import SimpleNamespace

from django.contrib.auth.models import AnonymousUser
from django.test import SimpleTestCase

from api.views import apply_search_context, store_search_context


class SearchContextTests(SimpleTestCase):
    class DummySession(dict):
        session_key = "test-session"
        modified = False

    def make_request(self):
        return SimpleNamespace(
            user=AnonymousUser(),
            session=self.DummySession(),
        )

    def test_context_reuses_server_query_plan_for_same_question(self):
        request = self.make_request()
        enrichment = {
            "queryIntent": {"intent": "definition", "focusTerms": ["竞价订单确认消息"]},
            "searchQuery": "竞价订单确认消息",
            "synonymExpansion": {"expandedTerms": []},
        }
        context_id = store_search_context(request, "test-project", "它是什么", enrichment, [{"chunkId": 3}])
        payload = apply_search_context(
            request,
            "test-project",
            {"query": "它是什么", "searchContextId": context_id},
        )
        self.assertTrue(payload["searchContextReused"])
        self.assertEqual(payload["searchQuery"], "竞价订单确认消息")
        self.assertEqual(payload["chunkIds"], [3])

    def test_context_is_not_reused_for_changed_question(self):
        request = self.make_request()
        context_id = store_search_context(
            request,
            "test-project",
            "问题一",
            {"queryIntent": {}, "searchQuery": "问题一", "synonymExpansion": {}},
            [],
        )
        payload = apply_search_context(
            request,
            "test-project",
            {"query": "问题二", "searchContextId": context_id},
        )
        self.assertNotIn("searchContextReused", payload)
