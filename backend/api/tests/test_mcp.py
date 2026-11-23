import json
import os
import tempfile
from pathlib import Path
from unittest.mock import patch

from django.test import TestCase

from api.models import Project, ProjectSourceRoot
from api.projects import get_project
from api.search_index import build_project_index


class MCPTests(TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {"WIKI_MCP_TOKEN": "test-token", "WIKI_MCP_ALLOWED_ORIGINS": ""})
        self.environment.start()
        self.addCleanup(self.environment.stop)
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        raw = root / "raw"
        raw.mkdir()
        (raw / "配置.md").write_text("# RC主机\n公共要求：先备份。\n## app_shell\napp_shell 使用键值对。\n" + "字段说明。" * 600, encoding="utf-8")
        self.project = Project.objects.create(name="测试知识库", root_path=str(root))
        ProjectSourceRoot.objects.create(project=self.project, path=str(raw), is_primary=True)
        build_project_index(get_project(str(self.project.id)))
        self.headers = {"HTTP_AUTHORIZATION": "Bearer test-token", "HTTP_ACCEPT": "application/json, text/event-stream"}

    def post(self, message, **headers):
        return self.client.post("/api/mcp/", data=json.dumps(message), content_type="application/json", **(self.headers | headers))

    def rpc(self, method, params=None):
        response = self.post({"jsonrpc": "2.0", "id": 1, "method": method, "params": params or {}})
        self.assertEqual(response.status_code, 200)
        return response.json()["result"]

    def call(self, name, arguments):
        result = self.rpc("tools/call", {"name": name, "arguments": arguments})
        self.assertNotIn("isError", result)
        return json.loads(result["content"][0]["text"])

    def test_initialize_discover_search_and_read_paginated_evidence_without_llm(self):
        # These model calls must never be reached by the MCP retrieval path.
        with patch("api.llm.analyze_query_intent", side_effect=AssertionError("LLM called")), patch("api.llm.answer_with_llm", side_effect=AssertionError("LLM called")):
            init = self.rpc("initialize", {"protocolVersion": "2025-06-18", "clientInfo": {"name": "test", "version": "1"}, "capabilities": {}})
            self.assertEqual(init["protocolVersion"], "2025-03-26")
            self.assertEqual(self.post({"jsonrpc": "2.0", "method": "notifications/initialized"}).status_code, 202)
            self.assertEqual(len(self.rpc("tools/list")["tools"]), 3)
            projects = self.call("wiki_list_projects", {})["projects"]
            self.assertIn({"id": str(self.project.id), "name": "测试知识库"}, projects)
            args = {"projectId": str(self.project.id), "query": "RC主机上的app_shell格式", "coreObjects": ["app_shell"], "scopeTerms": ["RC主机"]}
            hits = self.call("wiki_search", args)["results"]
            self.assertTrue(hits)
            self.assertNotIn("evidenceSourcePath", hits[0])
            self.assertEqual(hits[0]["retrieval"]["coreObjects"], ["app_shell"])
            evidence_args = {"projectId": str(self.project.id), "chunkId": hits[0]["chunkId"], "maxChars": 1000}
            page = self.call("wiki_read_evidence", evidence_args)
            content = page["content"]
            self.assertFalse(page["complete"])
            self.assertIn("先备份", content)
            while page["nextOffset"] is not None:
                page = self.call("wiki_read_evidence", evidence_args | {"offset": page["nextOffset"]})
                content += page["content"]
            whole = self.call("wiki_read_evidence", evidence_args | {"maxChars": 24000})
            self.assertEqual(content, whole["content"])
            self.assertTrue(whole["complete"])

    def test_auth_origin_and_transport_errors(self):
        message = {"jsonrpc": "2.0", "id": 1, "method": "ping"}
        self.assertEqual(self.post(message, HTTP_AUTHORIZATION="").status_code, 401)
        self.assertEqual(self.post(message, HTTP_ORIGIN="https://untrusted.example").status_code, 403)
        self.assertEqual(self.post(message, HTTP_MCP_PROTOCOL_VERSION="wrong").status_code, 400)
        self.assertEqual(self.post(message, HTTP_ACCEPT="application/json").status_code, 406)
        self.assertEqual(self.client.get("/api/mcp/", **self.headers).status_code, 405)
        with patch.dict(os.environ, {"WIKI_MCP_TOKEN": ""}):
            self.assertEqual(self.post(message).status_code, 503)

    def test_invalid_arguments_and_cross_project_chunk(self):
        bad_args = [
            {"projectId": "bad", "query": "x"},
            {"projectId": str(self.project.id), "query": "x", "limit": True},
            {"projectId": str(self.project.id), "query": "x", "scopeTerms": "RC"},
            {"projectId": str(self.project.id), "query": "x", "path": "../../secret"},
        ]
        for arguments in bad_args:
            self.assertTrue(self.rpc("tools/call", {"name": "wiki_search", "arguments": arguments})["isError"])
        empty_root = Path(self.directory.name) / "empty"
        empty_root.mkdir()
        other = Project.objects.create(name="empty", root_path=str(empty_root))
        result = self.rpc("tools/call", {"name": "wiki_read_evidence", "arguments": {"projectId": str(other.id), "chunkId": 1}})
        self.assertTrue(result["isError"])

    def test_rpc_validation_batches_and_notifications(self):
        self.assertEqual(self.post([]).json()["error"]["code"], -32600)
        self.assertEqual(self.post({"jsonrpc": "2.0", "id": True, "method": "ping"}).json()["error"]["code"], -32600)
        result = self.post([{ "jsonrpc": "2.0", "method": "notifications/initialized"}, {"jsonrpc": "2.0", "id": 3, "method": "ping"}]).json()
        self.assertEqual(result, [{"jsonrpc": "2.0", "id": 3, "result": {}}])
        response = self.client.post("/api/mcp/", data="{", content_type="application/json", **self.headers)
        self.assertEqual(response.json()["error"]["code"], -32700)
        with patch("api.mcp.call_tool") as call:
            self.assertEqual(self.post({"jsonrpc": "2.0", "method": "tools/call", "params": {}}).status_code, 202)
            call.assert_not_called()
