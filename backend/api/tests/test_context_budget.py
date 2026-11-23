from django.test import SimpleTestCase

from api.llm import build_context


class ContextBudgetTests(SimpleTestCase):
    def test_long_first_evidence_preserves_later_evidence(self):
        docs = [{"path": "first", "content": "A" * 25000},
                {"path": "second", "content": "B" * 3000}]
        context = build_context(docs)
        self.assertEqual(context.count("A"), 22000)
        self.assertEqual(context.count("B"), 2000)
        context = build_context(docs, max_chars=40000)
        self.assertEqual(context.count("A"), 25000)
        self.assertEqual(context.count("B"), 3000)

    def test_insufficient_budget_shares_reserve(self):
        docs = [{"content": letter * 3000} for letter in "ABC"]
        context = build_context(docs, max_chars=3000)
        for letter in "ABC":
            self.assertEqual(context.count(letter), 1000)
