import tempfile
import json
import sys
from pathlib import Path
from unittest.mock import patch

from django.test import SimpleTestCase

from tools.ingest_project import (
    ModelRateLimitError,
    build_ingestion_payload,
    chat_json,
    discover_documents,
    limit_extraction_items,
    load_section_checkpoint,
    main as ingest_main,
    merge_items,
    merge_relations,
    post_process_aggregate,
    save_section_checkpoint,
    section_checkpoint_key,
    section_label,
    raise_for_model_status,
)
from api.projects import project_graph
from api.views import parse_progress
from api.targeted_evidence import resolve_raw_source_path


class WikiBuilderSectionTests(SimpleTestCase):
    def test_ingestion_discovery_ignores_office_lock_file(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            (raw / "规则.docx").write_bytes(b"document")
            (raw / "~$规则.docx").write_bytes(b"lock")

            self.assertEqual([path.name for path in discover_documents(raw, 0)], ["规则.docx"])

    def test_ingestion_uses_indexed_chunks_and_resumes_from_checkpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            project = Path(directory)
            raw = project / "raw" / "sources" / "制度"
            raw.mkdir(parents=True)
            (raw / "规则.md").write_text("# 第三章\n\n## 第五节\n\n标准原文内容。", encoding="utf-8")
            output = project / "ingestion.json"
            checkpoint = project / ".index" / "ingestion-checkpoints" / "test"
            argv = [
                "ingest_project.py", "--project-dir", str(project), "--allow-heuristic",
                "--ingestion-output", str(output), "--checkpoint-dir", str(checkpoint),
            ]
            with patch.object(sys, "argv", argv), patch("tools.ingest_project.LLM_API_KEY", ""):
                ingest_main()

            payload = json.loads(output.read_text(encoding="utf-8"))
            section = payload["sources"][0]["sections"][-1]
            self.assertTrue(section["chunkId"])
            self.assertTrue(section["structuralNodeId"])
            self.assertEqual(section["headingPath"], "制度 > 规则.md > 第三章 > 第五节")

            with (
                patch.object(sys, "argv", argv),
                patch("tools.ingest_project.LLM_API_KEY", ""),
                patch("tools.ingest_project.extract_section_with_llm", side_effect=AssertionError("不应重复抽取")),
            ):
                ingest_main()

    def test_section_checkpoint_round_trip_and_content_change_invalidation(self):
        with tempfile.TemporaryDirectory() as directory:
            section = {
                "text": "原始规则", "sectionId": "nod_1", "structuralNodeId": "nod_1",
                "chunkId": 1, "documentVersionId": 1, "contentHash": "original",
                "headingPath": "制度 > 规则.docx > 第一章",
            }
            key = section_checkpoint_key("规则.docx", section)
            result = {"summary": "摘要", "objects": [], "relations": []}
            save_section_checkpoint(Path(directory), key, "规则.docx", section, result)

            self.assertEqual(load_section_checkpoint(Path(directory), key), result)
            changed = {**section, "text": "修改后的规则", "contentHash": "changed"}
            self.assertNotEqual(section_checkpoint_key("规则.docx", changed), key)

    def test_model_429_is_reported_as_retryable_rate_limit(self):
        class Response:
            status_code = 429
            headers = {"Retry-After": "12"}
            text = "rate limit exceeded"

        with self.assertRaises(ModelRateLimitError) as raised:
            raise_for_model_status(Response())

        self.assertEqual(raised.exception.retry_after_seconds, 12)
        self.assertIn("请求过于频繁", str(raised.exception))

    def test_invalid_model_json_is_repaired_once(self):
        malformed = '{"summary":"摘要","objects" [{"name":"订单"}],"relations":[]}'
        repaired = '```json\n{"summary":"摘要","objects":[{"name":"订单"}],"relations":[]}\n```'

        with (
            patch("tools.ingest_project.LLM_STREAM", True),
            patch("tools.ingest_project.chat_text_stream", side_effect=[malformed, repaired]) as request,
        ):
            result = chat_json("抽取", status_label="测试块")

        self.assertEqual(result["objects"][0]["name"], "订单")
        self.assertEqual(request.call_count, 2)
        self.assertIn("JSON 修复器", request.call_args.args[0]["messages"][0]["content"])

    def test_structure_manifest_preserves_sections_and_object_mentions(self):
        sections = [{
            "text": "竞价订单确认消息由系统返回。",
            "sectionId": "nod_order", "structuralNodeId": "nod_order",
            "chunkId": 17, "documentVersionId": 3, "chunkNo": 16,
            "headingPath": "交易规则 > 接口说明.md > 交易接口 > 订单确认",
            "startOffset": 20, "endOffset": 35, "contentHash": "hash", "summary": "",
        }]
        aggregate = {"sources": [], "objects": {}, "relations": []}
        item = {"name": "竞价订单确认消息", "summary": "确认订单接收结果。", "keywords": ["订单确认"]}
        merge_items(
            aggregate["objects"], [item], "交易规则/接口说明.md", sections[0]["text"], sections[0]
        )
        aggregate["sources"].append({
            "name": "接口说明.md",
            "relativePath": "交易规则/接口说明.md",
            "sha1": "abc",
            "sections": sections,
        })
        manifest = build_ingestion_payload(aggregate)
        self.assertEqual(manifest["schemaVersion"], 3)
        self.assertEqual(manifest["sources"][0]["sections"][0]["chunkId"], 17)
        self.assertEqual(manifest["objects"][0]["mentions"][0]["sectionId"], sections[0]["sectionId"])
        self.assertEqual(manifest["objects"][0]["keywords"], ["订单确认"])
        self.assertEqual(manifest["objects"][0]["mentions"][0]["structuralNodeId"], "nod_order")

    def test_progress_only_accepts_completed_chunk_marker(self):
        self.assertIsNone(parse_progress("[文档 1/1] 读取并切分 example.docx"))
        self.assertIsNone(parse_progress("[块 1/12] example.docx · 第一章 LLM 开始接收"))
        self.assertEqual(
            parse_progress("PROGRESS chunks 3 12"),
            {"current": 3, "total": 12, "percent": 25, "unit": "chunks"},
        )

    def test_chunk_log_label_contains_global_progress_and_heading(self):
        label = section_label(7, 18, "交易规则/接口说明.docx", {"headingPath": ["竞价交易", "订单确认"]})
        self.assertEqual(label, "[块 7/18] 交易规则/接口说明.docx · 竞价交易 > 订单确认")

    def test_nested_source_resolution_uses_full_relative_path(self):
        with tempfile.TemporaryDirectory() as directory:
            raw = Path(directory)
            expected = raw / "交易规则" / "接口说明.md"
            other = raw / "其他规则" / "接口说明.md"
            expected.parent.mkdir(parents=True)
            other.parent.mkdir(parents=True)
            expected.write_text("expected", encoding="utf-8")
            other.write_text("other", encoding="utf-8")
            self.assertEqual(resolve_raw_source_path(raw, "交易规则/接口说明.md"), expected.resolve())

    def test_chunk_object_budget_is_capped_at_ten(self):
        extraction = {
            "objects": [
                {"name": f"概念{i}", "summary": "这是一个能够独立解释的完整领域概念说明。"}
                for i in range(8)
            ] + [
                {"name": f"实体{i}", "summary": "这是一个能够独立解释的完整领域实体说明。"}
                for i in range(8)
            ],
            "relations": [
                {"source": "概念0", "relation": "约束", "target": "实体0"},
                {"source": "概念7", "relation": "约束", "target": "实体7"},
            ],
        }
        limited = limit_extraction_items(extraction, 50)
        selected = limited["objects"]
        self.assertEqual(len(selected), 10)
        selected_names = {item["name"] for item in selected}
        self.assertTrue(all(
            relation["source"] in selected_names and relation["target"] in selected_names
            for relation in limited["relations"]
        ))

    def test_global_filter_ranks_items_and_keeps_only_explicit_relations(self):
        aggregate = {"sources": [], "objects": {}, "relations": []}
        strong = {"name": "竞价订单确认消息", "summary": "用于确认竞价订单已经被系统接收并记录处理结果的业务消息。", "keywords": ["订单确认"]}
        weak = {"name": "有关内容", "summary": "很短"}
        target = {"name": "交易系统", "summary": "负责接收、校验并处理竞价订单的核心领域系统。", "keywords": ["系统"]}
        merge_items(aggregate["objects"], [strong], "规则一.docx", "竞价订单确认消息由交易系统处理。")
        merge_items(aggregate["objects"], [strong], "规则二.docx", "交易系统处理竞价订单确认消息。")
        merge_items(aggregate["objects"], [weak], "规则一.docx", "有关内容。")
        merge_items(aggregate["objects"], [target], "规则一.docx", "交易系统处理竞价订单确认消息。")
        merge_relations(aggregate["relations"], [
            {
                "source": "竞价订单确认消息",
                "relation": "由其处理",
                "target": "交易系统",
                "qualifiers": {"scope": "集合竞价阶段", "unknown": "忽略"},
            }
        ], "规则一.docx")

        post_process_aggregate(aggregate, max_objects=10)

        self.assertIn("竞价订单确认消息", {item["name"] for item in aggregate["objects"].values()})
        self.assertNotIn("有关内容", {item["name"] for item in aggregate["objects"].values()})
        relations = aggregate["objects"]["竞价订单确认消息"]["relations"]
        self.assertEqual(relations[0]["type"], "由其处理")
        self.assertEqual(relations[0]["qualifiers"], {"scope": "集合竞价阶段"})
        self.assertEqual(relations[0]["sourceDocument"], ["规则一.docx"])

    def test_graph_does_not_parse_markdown_without_v6_index(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wiki = root / "wiki"
            concepts = wiki / "concepts"
            concepts.mkdir(parents=True)
            (concepts / "甲.md").write_text(
                '---\ntitle: "甲"\ntype: "concept"\nrelations:\n  - {"target": "wiki/concepts/乙.md", "type": "约束"}\n---\n# 甲\n- 约束：[[wiki/concepts/乙.md|乙]]',
                encoding="utf-8",
            )
            (concepts / "乙.md").write_text(
                '---\ntitle: "乙"\ntype: "concept"\nrelations:\n  - {"target": "wiki/concepts/甲.md", "type": "约束"}\n---\n# 乙\n- 约束：[[wiki/concepts/甲.md|甲]]',
                encoding="utf-8",
            )
            (wiki / "index.md").write_text(
                '---\ntitle: "Wiki Index"\ntype: "overview"\n---\n[[concepts/甲.md|甲]]\n[[concepts/乙.md|乙]]',
                encoding="utf-8",
            )
            project = {"id": "test-project", "path": str(root), "exportDir": str(wiki), "rawDir": str(root / "raw")}
            with patch("api.projects.get_project", return_value=project):
                graph = project_graph("test-project")

            self.assertEqual(graph, {"nodes": [], "links": []})
