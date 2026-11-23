import tempfile
import json
import zipfile
from pathlib import Path
from unittest import mock

from django.test import SimpleTestCase

from api.search_index import (
    build_project_index,
    connect_index,
    get_indexed_document,
    get_ingestion_documents,
    get_indexed_section_evidence,
    get_indexed_structural_node,
    get_project_graph,
    inspect_project_index,
    search_project_index,
    verify_project_index,
)
from api.exports import export_project_markdown
from api.views import prepare_compose


class SearchIndexTests(SimpleTestCase):
    def extraction_payload(self, project):
        chunk = next(
            value for value in get_ingestion_documents(project)[0]["sections"]
            if "竞价订单确认消息" in value["text"]
        )
        pointer = {
            "chunkId": chunk["chunkId"],
            "documentVersionId": chunk["documentVersionId"],
            "structuralNodeId": chunk["structuralNodeId"],
            "startOffset": chunk["startOffset"],
            "endOffset": chunk["endOffset"],
        }
        return {
            "schemaVersion": 3,
            "objects": [{
                "objectId": "obj_test_order_confirmation",
                "name": "竞价订单确认消息",
                "description": "用于答复委托受理状态，并说明失败原因。",
                "aliases": ["订单确认消息"],
                "keywords": ["委托回执"],
                "domainType": "message",
                "sources": ["子目录/接口说明.md"],
                "mentions": [{
                    **pointer,
                    "sourceRelativePath": "子目录/接口说明.md",
                    "evidence": "竞价订单确认消息由交易系统返回。失败时包含错误代码和拒绝原因。",
                    "summary": "确认订单处理结果。",
                }],
                "relations": [{
                    "target": "交易系统", "type": "由其返回",
                    "sourceDocument": ["子目录/接口说明.md"],
                    "qualifiers": {"scope": "竞价订单处理"},
                    "evidence": [pointer],
                }],
            }, {
                "objectId": "obj_test_trading_system",
                "name": "交易系统",
                "domainType": "system",
                "sources": ["子目录/接口说明.md"],
                "mentions": [],
                "relations": [],
            }],
        }

    def make_project(self, root):
        wiki = root / "wiki"
        raw = root / "原文"
        (wiki / "entities").mkdir(parents=True)
        (raw / "子目录").mkdir(parents=True)
        (wiki / "entities" / "竞价订单确认消息.md").write_text(
            """---
title: 竞价订单确认消息
type: message
aliases:
  - 订单确认消息
sources:
  - 子目录/接口说明.md
relations:
  - target: wiki/entities/交易系统.md
    type: 被人工修改的导出关系
    sourceDocument:
      - 子目录/接口说明.md
    qualifiers:
      scope: 竞价订单处理
---
# 竞价订单确认消息

## 摘要
竞价订单确认消息用于确认竞价订单是否被交易系统接收。
""",
            encoding="utf-8",
        )
        (wiki / "entities" / "交易系统.md").write_text(
            """---
title: 交易系统
type: system
sources:
  - 子目录/接口说明.md
relations: []
---
# 交易系统

## 摘要
交易系统负责处理竞价订单并返回确认消息。
""",
            encoding="utf-8",
        )
        (raw / "子目录" / "接口说明.md").write_text(
            """# 交易接口

## 竞价订单确认
竞价订单确认消息由交易系统返回。失败时包含错误代码和拒绝原因。
""",
            encoding="utf-8",
        )
        (wiki / "structure.json").write_text(
            json.dumps({
                "schemaVersion": 2,
                "sources": [{
                    "relativePath": "子目录/接口说明.md",
                    "sha1": "test",
                    "sections": [{
                        "sectionId": "sec_test_order_confirmation",
                        "chunkNo": 0,
                        "headingPath": ["交易接口", "竞价订单确认"],
                        "part": 1,
                        "contentHash": "",
                        "charCount": 38,
                        "summary": "竞价订单确认消息用于返回订单接收结果。",
                    }],
                }],
                "objects": [{
                    "objectId": "obj_test_order_confirmation",
                    "type": "entity",
                    "name": "竞价订单确认消息",
                    "canonicalName": "竞价订单确认消息",
                    "wikiPath": "wiki/entities/竞价订单确认消息.md",
                    "relations": [{
                        "target": "wiki/entities/交易系统.md",
                        "type": "由其返回",
                        "sourceDocument": ["子目录/接口说明.md"],
                        "qualifiers": {"scope": "竞价订单处理"},
                    }],
                    "mentions": [{
                        "sectionId": "sec_test_order_confirmation",
                        "sourceRelativePath": "子目录/接口说明.md",
                        "sourceChunkNo": 0,
                        "headingPath": ["交易接口", "竞价订单确认"],
                        "part": 1,
                        "evidence": "竞价订单确认消息由交易系统返回。失败时包含错误代码和拒绝原因。",
                        "summary": "确认订单处理结果。",
                    }],
                }, {
                    "objectId": "obj_test_trading_system",
                    "type": "entity",
                    "name": "交易系统",
                    "canonicalName": "交易系统",
                    "wikiPath": "wiki/entities/交易系统.md",
                    "relations": [],
                    "mentions": [],
                }],
            }, ensure_ascii=False),
            encoding="utf-8",
        )
        return {
            "id": "test-project",
            "name": "测试项目",
            "path": str(root),
            "exportDir": str(wiki),
            "rawDir": str(raw),
        }

    def make_word_project(self, root):
        raw = root / "raw" / "sources"
        raw.mkdir(parents=True)
        document = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>目录</w:t></w:r></w:p>
<w:p><w:r><w:t>第三章 交易规则 ........ 3</w:t></w:r></w:p>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>历史修订记录</w:t></w:r></w:p>
<w:p><w:r><w:t>V1.0 初始化文档</w:t></w:r></w:p>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>第三章 交易规则</w:t></w:r></w:p>
<w:p><w:r><w:t>本章公共规定适用于本章所有交易方式。</w:t></w:r></w:p>
<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>第五节 集合竞价</w:t></w:r></w:p>
<w:p><w:r><w:t>3.5.2 集合竞价成交价格应遵守完整条款措辞。</w:t></w:r></w:p>
<w:p><w:pPr><w:numPr><w:ilvl w:val="0"/><w:numId w:val="1"/></w:numPr></w:pPr><w:r><w:t>申报应在规定时间内提交</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>交易方式</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>时间</w:t></w:r></w:p></w:tc></w:tr><w:tr><w:tc><w:p><w:r><w:t>集合竞价</w:t></w:r></w:p></w:tc><w:tc><w:p><w:r><w:t>9:15-9:25</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:pPr><w:pStyle w:val="Heading2"/></w:pPr><w:r><w:t>第六节 连续竞价</w:t></w:r></w:p>
<w:p><w:r><w:t>连续竞价内容不应进入第五节证据。</w:t></w:r></w:p>
</w:body></w:document>"""
        styles = """<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:style w:type="paragraph" w:styleId="Heading1"><w:name w:val="标题 1"/><w:pPr><w:outlineLvl w:val="0"/></w:pPr></w:style>
<w:style w:type="paragraph" w:styleId="Heading2"><w:name w:val="标题 2"/><w:basedOn w:val="Heading1"/><w:pPr><w:outlineLvl w:val="1"/></w:pPr></w:style>
</w:styles>"""
        numbering = """<?xml version="1.0" encoding="UTF-8"?>
<w:numbering xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:abstractNum w:abstractNumId="0"><w:lvl w:ilvl="0"><w:start w:val="1"/><w:numFmt w:val="decimal"/><w:lvlText w:val="%1."/></w:lvl></w:abstractNum>
<w:num w:numId="1"><w:abstractNumId w:val="0"/></w:num>
</w:numbering>"""
        with zipfile.ZipFile(raw / "交易规则.docx", "w") as archive:
            archive.writestr("word/document.xml", document)
            archive.writestr("word/styles.xml", styles)
            archive.writestr("word/numbering.xml", numbering)
        return {
            "id": "word-project", "name": "Word 项目", "path": str(root),
            "rawDir": str(raw), "exportDir": str(root / "exports" / "wiki"),
        }

    def test_v7_word_tree_and_complete_section_evidence(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_word_project(Path(directory))
            built = build_project_index(project)
            self.assertTrue(built["ok"])
            self.assertEqual(built["indexVersion"], 7)
            connection = connect_index(project)
            chapter = connection.execute(
                "SELECT * FROM structural_nodes WHERE node_type='section' AND heading='第三章 交易规则'"
            ).fetchone()
            section = connection.execute(
                "SELECT * FROM structural_nodes WHERE node_type='section' AND heading='第五节 集合竞价'"
            ).fetchone()
            self.assertEqual(section["parent_id"], chapter["id"])
            self.assertFalse(connection.execute(
                "SELECT 1 FROM structural_nodes WHERE node_type='section' AND heading LIKE '3.5.2%'"
            ).fetchone())
            child_types = {
                row[0] for row in connection.execute(
                    "SELECT node_type FROM structural_nodes WHERE parent_id=?", (section["id"],)
                )
            }
            self.assertTrue({"paragraph", "list", "table"} <= child_types)
            chunk = connection.execute(
                "SELECT * FROM chunks WHERE structural_node_id=? ORDER BY chunk_no LIMIT 1", (section["id"],)
            ).fetchone()
            self.assertIn("完整条款措辞", chunk["content"])
            self.assertNotIn("连续竞价内容", chunk["content"])
            version = connection.execute("SELECT extracted_text FROM document_versions LIMIT 1").fetchone()
            self.assertNotIn("第三章 交易规则 ........ 3", version["extracted_text"])
            self.assertNotIn("V1.0 初始化文档", version["extracted_text"])
            connection.close()

            evidence = get_indexed_section_evidence(project, [chunk["id"]])
            self.assertEqual(len(evidence), 1)
            self.assertEqual(
                evidence[0]["heading_path"],
                "交易规则.docx > 第三章 交易规则 > 第五节 集合竞价",
            )
            self.assertIn("本章公共规定适用于本章所有交易方式", evidence[0]["content"])
            self.assertIn("3.5.2 集合竞价成交价格应遵守完整条款措辞", evidence[0]["content"])
            self.assertIn("1. 申报应在规定时间内提交", evidence[0]["content"])
            self.assertIn("| 集合竞价 | 9:15-9:25 |", evidence[0]["content"])
            self.assertNotIn("连续竞价内容不应进入第五节证据", evidence[0]["content"])
            graph_section = get_indexed_structural_node(project, section["id"])
            self.assertIn("本章公共规定适用于本章所有交易方式", graph_section["content"])
            self.assertIn("命中章节完整原文", graph_section["content"])

            payload = {
                "paths": ["raw/sources/交易规则.docx"], "chunkIds": [chunk["id"]],
                "queryIntent": {"intent": "source", "focusTerms": []},
                "searchQuery": "集合竞价", "synonymExpansion": {"expandedTerms": []},
            }
            with mock.patch("api.views.get_project", return_value=project):
                prepared = prepare_compose("word-project", payload, "集合竞价完整原文")
            self.assertEqual(len(prepared["llmDocuments"]), 1)
            self.assertIn("命中章节完整原文", prepared["llmDocuments"][0]["content"])
            self.assertIn("本章公共规定", prepared["llmDocuments"][0]["content"])
            self.assertNotIn("连续竞价内容", prepared["llmDocuments"][0]["content"])

    def test_build_search_and_cached_content(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(Path(directory))
            initial = build_project_index(project)
            self.assertTrue(initial["ok"])
            self.assertTrue(initial["atomicSwap"])
            built = build_project_index(project, extracted_knowledge=self.extraction_payload(project))
            self.assertTrue(built["ok"])
            self.assertEqual(built["documents"], 1)
            self.assertEqual(built["documentVersions"], 1)
            self.assertGreater(built["structuralNodes"], 1)
            connection = connect_index(project)
            document_columns = {row[1] for row in connection.execute("PRAGMA table_info(documents)")}
            chunk_columns = {row[1] for row in connection.execute("PRAGMA table_info(chunks)")}
            tables = {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({"project_id", "relative_path", "mime_type", "status"} <= document_columns)
            self.assertIn("page_no", chunk_columns)
            self.assertIn("document_version_id", chunk_columns)
            self.assertIn("structural_node_id", chunk_columns)
            self.assertTrue({
                "document_versions", "structural_nodes",
                "knowledge_objects", "object_terms", "object_sources", "object_mentions",
                "claims", "claim_evidence",
            } <= tables)
            self.assertFalse({
                "entities", "entity_sources", "entity_chunk_links", "wiki_sections",
                "wiki_section_chunk_links", "wiki_object_mentions",
            } & tables)
            self.assertEqual(connection.execute("SELECT count(*) FROM knowledge_objects").fetchone()[0], 2)
            self.assertEqual(
                connection.execute("SELECT count(*) FROM object_terms WHERE term_kind='keyword'").fetchone()[0], 1
            )
            self.assertEqual(connection.execute("SELECT count(*) FROM claims").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT count(*) FROM claim_evidence").fetchone()[0], 1)
            evidence = connection.execute(
                "SELECT document_version_id, structural_node_id, start_offset, end_offset FROM claim_evidence"
            ).fetchone()
            self.assertTrue(evidence["document_version_id"])
            self.assertTrue(evidence["structural_node_id"])
            self.assertLess(evidence["start_offset"], evidence["end_offset"])
            connection.close()

    def test_admin_index_inspector_exposes_chunks_and_extraction(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(Path(directory))
            build_project_index(project)
            build_project_index(project, extracted_knowledge=self.extraction_payload(project))

            listing = inspect_project_index(project)
            self.assertTrue(listing["ok"])
            document_id = listing["documents"][0]["id"]
            document = inspect_project_index(project, document_id=document_id)
            self.assertTrue(document["sections"])
            target = next(chunk for chunk in document["chunks"] if "竞价订单确认消息" in chunk["content"])
            self.assertGreater(target["charCount"], 0)

            detail = inspect_project_index(project, chunk_id=target["id"])
            self.assertEqual(detail["chunk"]["id"], target["id"])
            extracted = next(item for item in detail["objects"] if item["name"] == "竞价订单确认消息")
            self.assertIn("用于答复委托受理状态", extracted["description"])
            self.assertIn("订单确认消息", extracted["aliases"])
            self.assertTrue(detail["claims"])
            self.assertIn("命中章节完整原文", detail["answerEvidence"]["content"])
            graph = get_project_graph(project)
            claim_link = next(link for link in graph["links"] if link.get("claimId"))
            self.assertEqual(claim_link["type"], "由其返回")
            self.assertEqual(claim_link["qualifiers"], {"scope": "竞价订单处理"})
            self.assertEqual(claim_link["evidenceCount"], 1)
            object_node = next(node for node in graph["nodes"] if node["id"] == "object:obj_test_order_confirmation")
            self.assertIn("用于答复委托受理状态", object_node["content"])
            self.assertIn("订单确认消息", object_node["aliases"])
            self.assertTrue(any("竞价订单确认消息 由其返回 交易系统" in value for value in object_node["claims"]))
            mention_link = next(link for link in graph["links"] if link["type"] == "mention")
            section_node = next(node for node in graph["nodes"] if node["id"] == mention_link["target"])
            self.assertEqual(section_node["label"], "子目录 > 接口说明.md > 交易接口 > 竞价订单确认")
            self.assertTrue(any(link["type"] == "mention" for link in graph["links"]))
            self.assertTrue(any(link["type"] == "source" for link in graph["links"]))
            self.assertTrue(any(
                link["type"] == "belongs-to"
                and link["source"] == section_node["id"]
                and link["target"].startswith("section:")
                for link in graph["links"]
            ))
            result = search_project_index(project, "竞价订单确认消息", 5)
            self.assertTrue(result["results"])
            self.assertEqual(result["results"][0]["title"], "接口说明")
            self.assertEqual(result["results"][0]["matchKind"], "object-source-structured")
            self.assertIn("竞价订单确认消息", result["results"][0]["retrieval"]["objectNames"])
            self.assertEqual(
                result["results"][0]["retrieval"]["chain"][:3],
                ["知识对象「竞价订单确认消息」", "抽取 Mention", "当前原文 Chunk"],
            )
            self.assertEqual(result["results"][0]["retrieval"]["indexTerms"], [])
            self.assertIn("竞价订单确认消息", result["results"][0]["retrieval"]["reason"])
            self.assertEqual(result["index"]["retrievalMode"], "object-linked")
            self.assertFalse(result["index"]["globalFtsUsed"])
            self.assertTrue(any(item["matchKind"] == "object-source-structured" for item in result["results"]))
            supplemental = search_project_index(project, "交易系统", 5)
            self.assertTrue(any(item["matchKind"] == "object-name-match" for item in supplemental["results"]))
            constrained = search_project_index(project, "竞价订单确认消息失败原因", 5)
            # “失败时…拒绝原因” does not continuously contain “失败原因”.
            self.assertEqual(constrained["index"]["retrievalMode"], "global-fts")
            self.assertTrue(constrained["index"]["globalFtsUsed"])
            self.assertTrue(all(item.get("retrieval", {}).get("chain") for item in constrained["results"]))
            description_hit = search_project_index(project, "委托受理状态", 5)
            self.assertTrue(description_hit["results"])
            self.assertEqual(description_hit["results"][0]["matchKind"], "object-source-structured")
            self.assertEqual(description_hit["results"][0]["chunkId"], result["results"][0]["chunkId"])
            keyword_hit = search_project_index(project, "委托回执", 5)
            self.assertTrue(keyword_hit["results"])
            self.assertEqual(keyword_hit["results"][0]["matchKind"], "object-source-structured")
            fallback = search_project_index(project, "不存在术语", 5)
            self.assertTrue(fallback["index"]["globalFtsUsed"])
            raw = get_indexed_document(project, "raw/sources/子目录/接口说明.md")
            self.assertIn("错误代码", raw["extracted_text"])
            self.assertTrue(verify_project_index(project)["ok"])
            with mock.patch("api.search_index.read_indexable_document", side_effect=AssertionError("query reparsed a file")):
                cached_result = search_project_index(project, "竞价订单确认消息", 5)
                self.assertTrue(cached_result["results"])

    def test_incremental_build_skips_unchanged_files(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(Path(directory))
            build_project_index(project)
            second = build_project_index(project)
            self.assertEqual(second["indexed"], 0)
            self.assertEqual(second["skipped"], 1)

    def test_office_lock_file_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_word_project(Path(directory))
            (Path(project["rawDir"]) / "~$交易规则.docx").write_bytes(b"not a zip file")

            built = build_project_index(project)

            self.assertTrue(built["ok"])
            self.assertEqual(built["documents"], 1)

    def test_v7_runtime_does_not_need_wiki_export(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            self.assertTrue(build_project_index(project)["ok"])
            self.assertTrue(build_project_index(project, extracted_knowledge=self.extraction_payload(project))["ok"])
            for path in sorted((root / "wiki").rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            rebuilt = build_project_index(project)
            self.assertTrue(rebuilt["ok"])
            self.assertEqual(rebuilt["documents"], 1)
            self.assertEqual(rebuilt["knowledgeObjects"], 2)
            self.assertTrue(search_project_index(project, "竞价订单确认消息", 5)["results"])
            graph = get_project_graph(project)
            self.assertTrue(any(node["type"] == "section" for node in graph["nodes"]))
            self.assertTrue(any(link["type"] == "mention" for link in graph["links"]))

    def test_document_change_creates_version_and_keeps_old_structure(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(Path(directory))
            first = build_project_index(project)
            self.assertTrue(build_project_index(project, extracted_knowledge=self.extraction_payload(project))["ok"])
            raw_path = Path(project["rawDir"]) / "子目录" / "接口说明.md"
            raw_path.write_text(raw_path.read_text(encoding="utf-8") + "\n\n## 新章节\n新增规则。", encoding="utf-8")
            second = build_project_index(project)
            self.assertTrue(second["ok"])
            self.assertEqual(second["documentVersions"], 2)
            self.assertGreater(second["structuralNodes"], first["structuralNodes"])
            self.assertEqual(second["knowledgeObjects"], 2)  # Historical extraction remains valid.
            self.assertTrue(verify_project_index(project)["ok"])

    def test_direct_extraction_builds_without_wiki_or_structure_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            project = self.make_project(root)
            for path in sorted((root / "wiki").rglob("*"), reverse=True):
                if path.is_file():
                    path.unlink()
                elif path.is_dir():
                    path.rmdir()
            self.assertTrue(build_project_index(project)["ok"])
            payload = self.extraction_payload(project)
            built = build_project_index(project, extracted_knowledge=payload)
            self.assertTrue(built["ok"])
            self.assertEqual(built["documents"], 1)
            self.assertEqual(built["knowledgeObjects"], 2)
            self.assertEqual(built["claims"], 1)
            self.assertTrue(search_project_index(project, "竞价订单确认消息", 5)["results"])
            self.assertTrue(verify_project_index(project)["ok"])
            exported = export_project_markdown(project)
            export_path = Path(exported["path"])
            self.assertEqual(exported["objects"], 2)
            self.assertTrue((export_path / "objects").is_dir())
            self.assertFalse((export_path / "structure.json").exists())
            (export_path / "index.md").write_text("人工修改", encoding="utf-8")
            export_project_markdown(project)
            self.assertIn("Knowledge Export", (export_path / "index.md").read_text(encoding="utf-8"))

    def test_version_upgrade_keeps_previous_index_backup(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(Path(directory))
            self.assertTrue(build_project_index(project)["ok"])
            connection = connect_index(project, create=True)
            connection.execute("UPDATE index_meta SET value='6' WHERE key='index_version'")
            connection.commit()
            connection.close()
            verification = verify_project_index(project)
            self.assertFalse(verification["ok"])
            self.assertFalse(verification["versionOk"])

            upgraded = build_project_index(project)

            self.assertTrue(upgraded["ok"])
            self.assertTrue(Path(upgraded["previousIndexBackup"]).is_file())
            self.assertEqual(upgraded["indexVersion"], 7)

    def test_failed_incremental_build_keeps_previous_valid_index(self):
        with tempfile.TemporaryDirectory() as directory:
            project = self.make_project(Path(directory))
            build_project_index(project)
            raw_path = Path(project["rawDir"]) / "子目录" / "接口说明.md"
            raw_path.write_text("已修改但解析失败", encoding="utf-8")
            with mock.patch("api.search_index.read_indexable_document", side_effect=RuntimeError("parser failed")):
                result = build_project_index(project)
            self.assertFalse(result["ok"])
            self.assertFalse(result["applied"])
            cached = get_indexed_document(project, "raw/sources/子目录/接口说明.md")
            self.assertIn("错误代码", cached["extracted_text"])
            self.assertTrue(verify_project_index(project)["ok"])
