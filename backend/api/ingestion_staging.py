"""Two-phase ingestion: retained Markdown first, private index second."""
import json
import os
import sqlite3
import time
import uuid
from pathlib import Path

from .file_paths import fs_path
from . import search_index as index


def save_report(directory, report):
    report["totalDocuments"] = len(report["documents"])
    report["succeededDocuments"] = sum(row["status"] == "succeeded" for row in report["documents"])
    report["failedDocuments"] = sum(row["status"].endswith("_failed") for row in report["documents"])
    report["pendingDocuments"] = report["totalDocuments"] - report["succeededDocuments"] - report["failedDocuments"]
    report["convertedDocuments"] = sum(row["status"] not in {"pending", "conversion_failed"} for row in report["documents"])
    report["conversionFailedDocuments"] = sum(row["status"] == "conversion_failed" for row in report["documents"])
    report["indexFailedDocuments"] = sum(row["status"] == "index_failed" for row in report["documents"])
    report["extractionFailedDocuments"] = sum(row["status"] == "extraction_failed" for row in report["documents"])
    report["publicationFailedDocuments"] = sum(row["status"] == "publication_failed" for row in report["documents"])
    report["historicalVersions"] = sum(not row.get("isLive", True) for row in report["documents"])
    directory = fs_path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".write-{uuid.uuid4().hex}.tmp"
    try:
        temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temporary, directory / "manifest.json")
    finally:
        temporary.unlink(missing_ok=True)


def prepare_markdown(project, directory, selected_refs=None):
    items = index.discover_project_documents(project)
    if selected_refs is not None:
        items = [item for item in items if item.get("live_source") in selected_refs]
    report = {"artifactDir": str(directory), "manifestPath": str(Path(directory) / "manifest.json"),
              "phase": "conversion", "documents": [
                  {"sourceRef": item["relative_path"], "originalSource": item.get("edition", {}).get("source_ref", item["relative_path"]),
                   "isLive": bool(item.get("live_source")), "status": "pending"} for item in items]}
    save_report(directory, report)
    prepared = []
    for item, parsed, error in index.parse_documents_in_order(items, project, markdown_dir=Path(directory) / "markdown"):
        record = {"sourceRef": item["relative_path"], "originalSource": item.get("edition", {}).get("source_ref", item["relative_path"]),
                  "isLive": bool(item.get("live_source")),
                  "sourceHash": item.get("edition", {}).get("file_hash"),
                  "markdownPath": item.get("markdownPath"), "status": "conversion_failed" if error else "converted"}
        if not error and not parsed["text"].strip():
            error = ValueError("转换结果没有可解析正文")
            record["status"] = "conversion_failed"
        if error:
            record.update(errorType=type(error).__name__, error=str(error)[:2000])
        else:
            prepared.append((item, parsed))
        next(row for row in report["documents"] if row["sourceRef"] == record["sourceRef"]).update(record)
        save_report(directory, report)
    report["phase"] = "conversion_complete"
    save_report(directory, report)
    return items, prepared, report


def remove_documents(connection, document_ids):
    for document_id in document_ids:
        index.delete_document(connection, document_id)
    # Cascades remove evidence and profiles. Remove their now orphaned objects too.
    connection.execute("DELETE FROM claims WHERE NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.claim_id=claims.claim_id)")
    connection.execute("DELETE FROM knowledge_objects WHERE NOT EXISTS (SELECT 1 FROM object_sources s WHERE s.object_id=knowledge_objects.id) AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.subject_object_id=knowledge_objects.id OR c.object_object_id=knowledge_objects.id)")


def stage_prepared_import(project, stage_path, items, prepared, report):
    """Only consumes converted data. SQLite writes stay on the calling thread."""
    Path(stage_path).parent.mkdir(parents=True, exist_ok=True)
    with index._build_lock(project):
        source = index.connect_index(project)
        baseline = index.get_meta(source, "last_build_at") if source else None
        destination = sqlite3.connect(stage_path)
        try:
            if source:
                source.backup(destination)
            else:
                empty = sqlite3.connect(":memory:")
                try:
                    empty.backup(destination)
                finally:
                    empty.close()
        finally:
            destination.close()
            if source:
                source.close()
    staged = {**project, "_indexPath": str(stage_path)}
    connection = index.connect_index(staged, create=True)
    records = {row["sourceRef"]: row for row in report["documents"]}
    ids = []
    try:
        with connection:
            # Replace only attempted editions; unrelated documents and historical
            # editions keep their existing content and evidence.
            prepared_paths = {item["virtual_path"] for item, _ in prepared}
            for item in items:
                old = connection.execute("SELECT id FROM documents WHERE virtual_path=?", (item["virtual_path"],)).fetchone()
                item["previousId"] = old[0] if old else None
                if old and item["virtual_path"] not in prepared_paths:
                    remove_documents(connection, [old[0]])
            for item, parsed in prepared:
                connection.execute("SAVEPOINT prepared_document")
                try:
                    if item["previousId"]:
                        document_id = item["previousId"]
                        connection.execute("DELETE FROM claim_evidence WHERE document_id=?", (document_id,))
                        connection.execute("DELETE FROM object_sources WHERE document_id=?", (document_id,))
                        connection.execute("DELETE FROM object_version_profiles WHERE document_id=?", (document_id,))
                        connection.execute("DELETE FROM object_mentions WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id=?)", (document_id,))
                    index.index_document(connection, project, item, item["previousId"], parsed=parsed)
                    document_id = connection.execute("SELECT id FROM documents WHERE virtual_path=?", (item["virtual_path"],)).fetchone()[0]
                    if not connection.execute("SELECT 1 FROM chunks WHERE document_id=?", (document_id,)).fetchone():
                        raise ValueError("解析后没有可抽取正文切片（可能仅有标题或空表），本文件未入库")
                    connection.execute("RELEASE SAVEPOINT prepared_document")
                    ids.append(document_id)
                    records[item["relative_path"]]["status"] = "indexed"
                except Exception as exc:
                    connection.execute("ROLLBACK TO SAVEPOINT prepared_document")
                    connection.execute("RELEASE SAVEPOINT prepared_document")
                    if item["previousId"]:
                        remove_documents(connection, [item["previousId"]])
                    records[item["relative_path"]].update(status="index_failed", errorType=type(exc).__name__, error=str(exc)[:2000])
            index.set_meta(connection, "document_import_full", False)
            index.set_meta(connection, "document_import_documents", ids)
            index.set_meta(connection, "document_import_sources", [row["sourceRef"] for row in report["documents"] if row["status"] == "indexed"])
            index.set_meta(connection, "document_import_validation", [
                {"source_ref": item["live_source"], "file_hash": item["edition"]["file_hash"], "relative_path": item["relative_path"]}
                for item, _ in prepared if item.get("live_source") and records[item["relative_path"]]["status"] == "indexed"])
            index.set_meta(connection, "index_version", index.INDEX_VERSION)
            index.set_meta(connection, "parser_version", index.PARSER_VERSION)
            index.set_meta(connection, "build_complete", True)
            index.set_meta(connection, "last_build_at", time.time())
    finally:
        connection.close()
    return staged, baseline


def finish_staging(project, report, failed_refs):
    connection = index.connect_index(project, create=True)
    try:
        with connection:
            ids = []
            for row in report["documents"]:
                if row["status"] != "indexed":
                    continue
                document = connection.execute("SELECT id FROM documents WHERE relative_path=?", (row["sourceRef"],)).fetchone()
                if row["sourceRef"] in failed_refs:
                    failure = next((item for item in report.get("chunkFailures", []) if item["sourceRef"] == row["sourceRef"]), {})
                    row.update(status="extraction_failed", errorType=failure.get("errorType"),
                               error=failure.get("message") or "部分切片抽取失败，整份文档已从本次索引排除")
                    remove_documents(connection, [document[0]])
                else:
                    row["status"] = "succeeded"
                    ids.append(document[0])
            index.set_meta(connection, "document_import_documents", ids)
            index.set_meta(connection, "document_import_sources", [row["sourceRef"] for row in report["documents"] if row["status"] == "succeeded"])
            index.set_meta(connection, "document_import_validation", [row for row in index.get_meta(connection, "document_import_validation", []) if row["relative_path"] not in failed_refs])
            remove_documents(connection, [])
    finally:
        connection.close()
    report.update(phase="ready_to_publish", succeededDocuments=sum(row["status"] == "succeeded" for row in report["documents"]))
    report["failedDocuments"] = len(report["documents"]) - report["succeededDocuments"]
    save_report(report["artifactDir"], report)


def reconcile_publication(project, report):
    """Count actual committed editions separately from logical/default documents."""
    connection = index.connect_index(project)
    if connection is None:
        raise RuntimeError("发布校验失败：无法读取已发布索引")
    try:
        for row in report["documents"]:
            if row["status"] != "succeeded":
                continue
            actual = connection.execute("""SELECT e.uid,e.logical_id,e.is_default,
                (SELECT count(*) FROM chunks c WHERE c.document_id=d.id) AS chunks
                FROM documents d JOIN document_editions e ON e.document_id=d.id
                WHERE d.relative_path=?""", (row["sourceRef"],)).fetchone()
            if actual is None or not actual["chunks"]:
                row.update(status="publication_failed", error="发布后未找到文件版本或正文切片，请重试")
            else:
                row.update(editionId=actual["uid"], logicalDocumentId=actual["logical_id"],
                           isDefaultVersion=bool(actual["is_default"]), chunkCount=actual["chunks"])
        totals = connection.execute("SELECT count(*),count(DISTINCT logical_id),sum(is_default) FROM document_editions").fetchone()
        report.update(phase="published", projectVersions=totals[0], projectDocuments=totals[1],
                      projectDefaultVersions=totals[2] or 0,
                      publishedDocuments=sum(row["status"] == "succeeded" for row in report["documents"]))
    finally:
        connection.close()
    save_report(report["artifactDir"], report)
