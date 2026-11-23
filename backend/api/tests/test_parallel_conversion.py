import json
import tempfile
import threading
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase

from api import search_index as index
from api.document_conversion import conversion_error_details


class ParallelConversionTests(SimpleTestCase):
    def items(self, root, count=3):
        items = []
        for number in range(count):
            path = root / f"{number}.md"
            path.write_text(f"# 标题{number}\n正文{number}。", encoding="utf-8")
            items.append({"path": path, "relative_path": path.name, "virtual_path": f"raw/sources/{path.name}"})
        return items

    def parse(self, path):
        return index.parse_text_document(path.read_text(encoding="utf-8"))

    def test_parsing_overlaps_but_results_and_database_writes_keep_order(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = self.items(root, 2)
            barrier = threading.Barrier(2)
            thread_ids = set()

            def parallel_parse(path):
                thread_ids.add(threading.get_ident())
                barrier.wait(2)
                return self.parse(path)

            with (patch.dict("os.environ", {"DOCUMENT_CONVERSION_CONCURRENCY": "2"}),
                  patch.object(index, "read_indexable_document", side_effect=parallel_parse)):
                parsed = list(index.parse_documents_in_order(items, {"path": directory}))
            self.assertEqual(len(thread_ids), 2)
            self.assertNotIn(threading.get_ident(), thread_ids)
            self.assertEqual([row[0]["relative_path"] for row in parsed], ["0.md", "1.md"])
            self.assertTrue(all(row[2] is None for row in parsed))

            original = index.index_document
            write_threads, names = [], []

            def write(*args, **kwargs):
                write_threads.append(threading.get_ident())
                names.append(args[2]["edition"]["source_ref"])
                return original(*args, **kwargs)

            with patch.object(index, "index_document", side_effect=write):
                result = index.build_project_index({"path": directory, "rawDir": directory})
            self.assertTrue(result["ok"], result)
            self.assertEqual(names, ["0.md", "1.md"])
            self.assertEqual(set(write_threads), {threading.get_ident()})

    def test_failed_file_does_not_repeat_successful_conversion_and_cache_invalidates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            items = self.items(root)

            def failing(path):
                if path.name == "1.md":
                    raise ValueError("invalid XML")
                return self.parse(path)

            with patch.object(index, "read_indexable_document", side_effect=failing):
                first = list(index.parse_documents_in_order(items, {"path": directory}))
            self.assertEqual(sum(row[2] is not None for row in first), 1)
            with patch.object(index, "read_indexable_document", side_effect=self.parse) as parse:
                second = list(index.parse_documents_in_order(items, {"path": directory}))
            self.assertTrue(all(row[2] is None for row in second))
            parse.assert_called_once_with(items[1]["path"])
            items[0]["path"].write_text("# 修改后的正文", encoding="utf-8")
            with patch.object(index, "read_indexable_document", side_effect=self.parse) as parse:
                list(index.parse_documents_in_order(items, {"path": directory}))
            parse.assert_called_once_with(items[0]["path"])
            with (patch.object(index, "PARSER_VERSION", index.PARSER_VERSION + "-changed"),
                  patch.object(index, "read_indexable_document", side_effect=self.parse) as parse):
                list(index.parse_documents_in_order(items, {"path": directory}))
            self.assertEqual(parse.call_count, 3)
            with patch.object(index, "read_indexable_document", side_effect=self.parse) as parse:
                list(index.parse_documents_in_order(items, {"path": directory}, use_cache=False))
            self.assertEqual(parse.call_count, 3)

    def test_wrapped_converter_reports_underlying_exception(self):
        inner = ValueError("invalid workbook XML")
        outer = RuntimeError("conversion failed")
        outer.attempts = [SimpleNamespace(exc_info=(ValueError, inner, None))]
        detail = conversion_error_details(outer)
        self.assertEqual([item["type"] for item in detail], ["RuntimeError", "ValueError"])
        self.assertIn("invalid workbook XML", json.dumps(detail))
