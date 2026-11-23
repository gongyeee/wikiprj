import hashlib
import json
import mimetypes
import os
import re
import sqlite3
import threading
import time
import uuid
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from .document_conversion import convert_to_markdown, conversion_event, SUPPORTED_RAW_SUFFIXES, CONVERTER_VERSION
from .file_paths import fs_path
from .document_versions import (
    create_version_schema, discover_editions, register_edition, save_catalog,
    apply_version_scope, scope_project, edition_metadata, record_profiles, backup_before_migration, migrate_legacy_editions,
)


INDEX_VERSION = 7
PARSER_VERSION = f"2026-10-28.1-{CONVERTER_VERSION}"
_BUILD_LOCKS = {}
_BUILD_LOCKS_GUARD = threading.Lock()


def project_root(project):
    return Path(project["path"]).resolve()


def project_raw_dir(project):
    if project.get("rawDir"):
        return Path(project["rawDir"]).resolve()
    root = project_root(project)
    for folder in ("resources", "原文"):
        candidate = root / folder
        if candidate.exists():
            return candidate.resolve()
    return (root / "raw" / "sources").resolve()


def index_path(project):
    if project.get("_indexPath"):
        return Path(project["_indexPath"])
    return project_root(project) / ".index" / "search.db"


def connect_index(project, create=False, path_override=None):
    path = Path(path_override) if path_override else index_path(project)
    if create:
        path.parent.mkdir(parents=True, exist_ok=True)
    elif not path.exists():
        return None
    if create:
        connection = sqlite3.connect(path, timeout=20)
    else:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=20)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    if create:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = NORMAL")
        create_schema(connection)
    if not create and "_versionScope" in project:
        try:
            apply_version_scope(connection, project["_versionScope"])
        except Exception:
            connection.close()
            raise
    return connection


def create_schema(connection):
    connection.executescript(
        """
        CREATE TABLE IF NOT EXISTS index_meta (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS documents (
            id INTEGER PRIMARY KEY,
            project_id TEXT NOT NULL,
            virtual_path TEXT NOT NULL UNIQUE,
            relative_path TEXT NOT NULL,
            absolute_path TEXT NOT NULL,
            document_type TEXT NOT NULL,
            title TEXT NOT NULL,
            suffix TEXT NOT NULL,
            mime_type TEXT NOT NULL,
            size INTEGER NOT NULL,
            mtime_ns INTEGER NOT NULL,
            content_hash TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            extracted_text TEXT NOT NULL,
            indexed_at REAL NOT NULL,
            status TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS document_versions (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            content_hash TEXT NOT NULL,
            parser_version TEXT NOT NULL,
            extracted_text TEXT NOT NULL,
            created_at REAL NOT NULL,
            UNIQUE(document_id, content_hash, parser_version)
        );
        CREATE INDEX IF NOT EXISTS document_versions_document_id ON document_versions(document_id);
        CREATE TABLE IF NOT EXISTS structural_nodes (
            id TEXT PRIMARY KEY,
            document_version_id INTEGER NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
            parent_id TEXT REFERENCES structural_nodes(id) ON DELETE CASCADE,
            node_type TEXT NOT NULL,
            ordinal INTEGER NOT NULL,
            heading TEXT NOT NULL DEFAULT '',
            heading_path TEXT NOT NULL DEFAULT '',
            page_no INTEGER,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            content_hash TEXT NOT NULL
        );
        CREATE INDEX IF NOT EXISTS structural_nodes_version_id ON structural_nodes(document_version_id);
        CREATE TABLE IF NOT EXISTS chunks (
            id INTEGER PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            document_version_id INTEGER NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
            structural_node_id TEXT REFERENCES structural_nodes(id) ON DELETE SET NULL,
            chunk_no INTEGER NOT NULL,
            heading_path TEXT NOT NULL,
            page_no INTEGER,
            start_offset INTEGER NOT NULL,
            end_offset INTEGER NOT NULL,
            content TEXT NOT NULL,
            content_hash TEXT NOT NULL,
            UNIQUE(document_id, chunk_no)
        );
        CREATE INDEX IF NOT EXISTS chunks_document_id ON chunks(document_id);
        CREATE TABLE IF NOT EXISTS knowledge_objects (
            id INTEGER PRIMARY KEY,
            object_key TEXT NOT NULL UNIQUE,
            canonical_name TEXT NOT NULL,
            normalized_name TEXT NOT NULL,
            domain_type TEXT NOT NULL,
            ontology_kind TEXT NOT NULL DEFAULT 'unspecified',
            description TEXT NOT NULL DEFAULT '',
            status TEXT NOT NULL DEFAULT 'candidate'
        );
        CREATE INDEX IF NOT EXISTS knowledge_objects_normalized_name ON knowledge_objects(normalized_name);
        CREATE TABLE IF NOT EXISTS object_terms (
            object_id INTEGER NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
            term TEXT NOT NULL,
            normalized_term TEXT NOT NULL,
            term_kind TEXT NOT NULL,
            provenance TEXT NOT NULL DEFAULT 'extracted',
            PRIMARY KEY(object_id, normalized_term)
        );
        CREATE INDEX IF NOT EXISTS object_terms_normalized_term ON object_terms(normalized_term);
        CREATE TABLE IF NOT EXISTS object_sources (
            object_id INTEGER NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            source_ref TEXT NOT NULL,
            PRIMARY KEY(object_id, document_id)
        );
        CREATE TABLE IF NOT EXISTS object_mentions (
            mention_id TEXT PRIMARY KEY,
            object_id INTEGER NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
            document_version_id INTEGER NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
            structural_node_id TEXT REFERENCES structural_nodes(id) ON DELETE SET NULL,
            raw_chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
            start_offset INTEGER NOT NULL DEFAULT 0,
            end_offset INTEGER NOT NULL DEFAULT 0,
            matched_text TEXT NOT NULL DEFAULT '',
            evidence TEXT NOT NULL DEFAULT '',
            extraction_summary TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0
        );
        CREATE INDEX IF NOT EXISTS object_mentions_object_id ON object_mentions(object_id);
        CREATE INDEX IF NOT EXISTS object_mentions_raw_chunk_id ON object_mentions(raw_chunk_id);
        CREATE TABLE IF NOT EXISTS claims (
            claim_id TEXT PRIMARY KEY,
            subject_object_id INTEGER NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
            predicate TEXT NOT NULL,
            object_object_id INTEGER REFERENCES knowledge_objects(id) ON DELETE CASCADE,
            literal_value TEXT NOT NULL DEFAULT '',
            qualifiers_json TEXT NOT NULL DEFAULT '{}',
            status TEXT NOT NULL DEFAULT 'candidate'
        );
        CREATE INDEX IF NOT EXISTS claims_subject ON claims(subject_object_id);
        CREATE INDEX IF NOT EXISTS claims_object ON claims(object_object_id);
        CREATE TABLE IF NOT EXISTS claim_evidence (
            claim_id TEXT NOT NULL REFERENCES claims(claim_id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            document_version_id INTEGER NOT NULL REFERENCES document_versions(id) ON DELETE CASCADE,
            structural_node_id TEXT REFERENCES structural_nodes(id) ON DELETE SET NULL,
            chunk_id INTEGER REFERENCES chunks(id) ON DELETE SET NULL,
            start_offset INTEGER NOT NULL DEFAULT 0,
            end_offset INTEGER NOT NULL DEFAULT 0,
            quoted_text TEXT NOT NULL DEFAULT '',
            score REAL NOT NULL DEFAULT 0,
            PRIMARY KEY(claim_id, document_id, chunk_id)
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
            title,
            relative_path,
            heading_path,
            tokens,
            content,
            tokenize='unicode61 remove_diacritics 2'
        );
        """
    )
    connection.executescript(
        """
        DROP TABLE IF EXISTS wiki_object_mentions;
        DROP TABLE IF EXISTS wiki_section_chunk_links;
        DROP TABLE IF EXISTS wiki_sections;
        DROP TABLE IF EXISTS entity_chunk_links;
        DROP TABLE IF EXISTS entity_sources;
        DROP TABLE IF EXISTS entity_aliases;
        DROP TABLE IF EXISTS entities;
        """
    )
    create_version_schema(connection)
    set_meta(connection, "index_version", INDEX_VERSION)
    set_meta(connection, "parser_version", PARSER_VERSION)
    set_meta(connection, "tokenizer_version", "unicode61+zh-bigram+zh-trigram-v1")
    set_meta_default(connection, "build_complete", False)
    connection.commit()


def set_meta(connection, key, value):
    connection.execute(
        "INSERT INTO index_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def set_meta_default(connection, key, value):
    connection.execute(
        "INSERT OR IGNORE INTO index_meta(key, value) VALUES(?, ?)",
        (key, json.dumps(value, ensure_ascii=False)),
    )


def get_meta(connection, key, default=None):
    row = connection.execute("SELECT value FROM index_meta WHERE key = ?", (key,)).fetchone()
    if not row:
        return default
    try:
        return json.loads(row["value"])
    except json.JSONDecodeError:
        return row["value"]


def _build_lock(project):
    key = str(index_path(project)).casefold()
    with _BUILD_LOCKS_GUARD:
        return _BUILD_LOCKS.setdefault(key, threading.Lock())


def build_project_index(project, force=False, extracted_knowledge=None):
    started = time.perf_counter()
    with _build_lock(project):
        target = index_path(project)
        current = index_database_version(target) == INDEX_VERSION
        if not target.exists() or not current:
            # V7 deliberately rebuilds from source; legacy knowledge projections are not imported.
            payload = extracted_knowledge if extracted_knowledge is not None else {"schemaVersion": 3, "objects": []}
            result = build_project_index_atomic(project, target, payload)
        else:
            backup_before_migration(project)
            connection = connect_index(project, create=True)
            try:
                result = build_connected_index(project, connection, force=force, extracted_knowledge=extracted_knowledge)
            finally:
                connection.close()
        if result.get("ok"):
            catalog_connection = connect_index(project)
            try:
                save_catalog(project, catalog_connection)
            finally:
                catalog_connection.close()
        return {
            "path": str(target),
            "elapsedMs": round((time.perf_counter() - started) * 1000),
            **result,
        }


def index_database_version(path):
    path = Path(path)
    if not path.exists():
        return None
    try:
        connection = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=5)
        row = connection.execute("SELECT value FROM index_meta WHERE key = 'index_version'").fetchone()
        connection.close()
        return json.loads(row[0]) if row else None
    except (sqlite3.Error, json.JSONDecodeError):
        return None


def stage_document_import(project, source_refs, stage_path):
    """Parse selected files in a private SQLite snapshot; live evidence stays usable."""
    refs = set(source_refs)
    available = {item["live_source"]: item for item in discover_project_documents(project) if item.get("live_source")}
    if not refs or refs - available.keys():
        raise ValueError("请选择 rawDir 内存在的原文相对路径")
    source = connect_index(project)
    if source is None:
        raise ValueError("按文档导入需要已有项目索引，请先完成首次摄入")
    if get_meta(source, "index_version") != INDEX_VERSION or not get_meta(source, "build_complete", False):
        source.close()
        raise ValueError("当前索引版本过旧或不完整，请先完成全量摄入")
    Path(stage_path).parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(stage_path)
    try:
        source.backup(destination)
    finally:
        source.close()
        destination.close()
    staged = {**project, "_indexPath": str(stage_path)}
    connection = connect_index(staged, create=True)
    baseline = get_meta(connection, "last_build_at")
    try:
        with connection:
            affected_objects = set()
            affected_claims = set()
            failures = []
            for item, parsed, parse_error in parse_documents_in_order([available[ref] for ref in sorted(refs)], project):
                if parse_error is not None:
                    failures.append({"path": item["virtual_path"], "error": str(parse_error)})
                    continue
                old = connection.execute("SELECT id FROM documents WHERE virtual_path=?", (item["virtual_path"],)).fetchone()
                if old:
                    document_id = old["id"]
                    affected_objects.update(row[0] for row in connection.execute("SELECT object_id FROM object_sources WHERE document_id=?", (document_id,)))
                    affected_claims.update(row[0] for row in connection.execute("SELECT claim_id FROM claim_evidence WHERE document_id=?", (document_id,)))
                    connection.execute("DELETE FROM claim_evidence WHERE document_id=?", (document_id,))
                    connection.execute("DELETE FROM object_mentions WHERE document_version_id IN (SELECT id FROM document_versions WHERE document_id=?)", (document_id,))
                    connection.execute("DELETE FROM object_sources WHERE document_id=?", (document_id,))
                    connection.execute("DELETE FROM object_version_profiles WHERE document_id=?", (document_id,))
                index_document(connection, project, item, old["id"] if old else None, parsed=parsed)
            if failures:
                raise ValueError(f"原文解析失败：{failures}")
            set_meta(connection, "document_import_full", False)
            set_meta(connection, "document_import_sources", sorted(refs))
            set_meta(connection, "document_import_documents", [
                connection.execute("SELECT id FROM documents WHERE virtual_path=?", (available[ref]["virtual_path"],)).fetchone()[0]
                for ref in sorted(refs)
            ])
            for claim_id in affected_claims:
                connection.execute("DELETE FROM claims WHERE claim_id=? AND NOT EXISTS (SELECT 1 FROM claim_evidence e WHERE e.claim_id=claims.claim_id)", (claim_id,))
            for object_id in affected_objects:
                connection.execute("DELETE FROM knowledge_objects WHERE id=? AND NOT EXISTS (SELECT 1 FROM object_sources s WHERE s.object_id=knowledge_objects.id) AND NOT EXISTS (SELECT 1 FROM claims c WHERE c.subject_object_id=knowledge_objects.id OR c.object_object_id=knowledge_objects.id)", (object_id,))
    finally:
        connection.close()
    return staged, baseline


def publish_document_import(project, stage_path, baseline, payload, source_assignments=None):
    staged = {**project, "_indexPath": str(stage_path)}
    connection = connect_index(staged, create=True)
    try:
        validation = get_meta(connection, "document_import_validation")
        if validation is None:
            validation = [connection.execute("SELECT source_ref, file_hash FROM document_editions WHERE document_id=?", (document_id,)).fetchone()
                          for document_id in get_meta(connection, "document_import_documents", [])]
        for edition in validation:
            source = fs_path(project_raw_dir(project) / edition["source_ref"])
            if not source.is_file() or hashlib.sha256(source.read_bytes()).hexdigest() != edition["file_hash"]:
                raise ValueError(f"抽取期间原文已修改，请重新导入：{edition['source_ref']}")
        with connection:
            from .ingestion_files import apply_source_assignments
            apply_source_assignments(connection, source_assignments)
            rebuild_knowledge_model(connection, payload, merge=not get_meta(connection, "document_import_full", False))
            set_meta(connection, "last_build_at", time.time())
        verification = verify_project_index(staged)
        if not verification.get("ok"):
            raise ValueError(f"文档更新校验失败：{verification}")
        with _build_lock(project):
            live = connect_index(project, create=True)
            try:
                if get_meta(live, "last_build_at") != baseline:
                    raise ValueError("抽取期间项目索引已更新，请重试此次文档导入")
                connection.backup(live)
                save_catalog(project, live)
            finally:
                live.close()
        return {"ok": True, **index_stats(connection)}
    finally:
        connection.close()


def stage_project_import(project, stage_path, selected_refs=None):
    """Full ingestion also publishes only after extraction and verification succeed."""
    staged = {**project, "_indexPath": str(stage_path)}
    if selected_refs:
        staged["_sourceRefs"] = set(selected_refs)
    Path(stage_path).parent.mkdir(parents=True, exist_ok=True)
    with _build_lock(project):
        backup_before_migration(project)
        source = connect_index(project)
        baseline = get_meta(source, "last_build_at") if source else None
        destination = sqlite3.connect(stage_path)
        try:
            if source:
                source.backup(destination)
            else:
                # Retrying a failed first import must not reuse its private index.
                empty = sqlite3.connect(":memory:")
                try:
                    empty.backup(destination)
                finally:
                    empty.close()
        finally:
            destination.close()
            if source:
                source.close()
    connection = connect_index(staged, create=True)
    try:
        result = build_connected_index(staged, connection)
        if not result.get("ok"):
            raise ValueError(f"原文解析失败：{result.get('failures')}")
        live = [item for item in discover_project_documents(staged) if item.get("live_source")
                and (not selected_refs or item["live_source"] in selected_refs)]
        with connection:
            set_meta(connection, "document_import_full", not bool(selected_refs))
            set_meta(connection, "document_import_sources", [item["live_source"] for item in live])
            set_meta(connection, "document_import_documents", [
                connection.execute("SELECT id FROM documents WHERE virtual_path=?", (item["virtual_path"],)).fetchone()[0]
                for item in live
            ])
        return staged, baseline
    finally:
        connection.close()


def build_project_index_atomic(project, target, extracted_knowledge=None):
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    for path in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
        if path.exists():
            path.unlink()
    connection = connect_index(project, create=True, path_override=temporary)
    try:
        result = build_connected_index(project, connection, force=True, extracted_knowledge=extracted_knowledge)
        if result["ok"]:
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    finally:
        connection.close()
    if not result["ok"]:
        for path in (temporary, Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
            if path.exists():
                path.unlink()
        result["atomicSwap"] = False
        return result
    previous_version = index_database_version(target)
    if target.exists() and previous_version is not None:
        backup = target.with_name(f"{target.name}.v{previous_version}.bak")
        if not backup.exists():
            source = sqlite3.connect(f"file:{target.as_posix()}?mode=ro", uri=True, timeout=20)
            destination = sqlite3.connect(backup, timeout=20)
            source.backup(destination)
            destination.close()
            source.close()
        result["previousIndexBackup"] = str(backup)
    for path in (Path(f"{target}-wal"), Path(f"{target}-shm")):
        if path.exists():
            path.unlink()
    os.replace(temporary, target)
    for path in (Path(f"{temporary}-wal"), Path(f"{temporary}-shm")):
        if path.exists():
            path.unlink()
    result["atomicSwap"] = True
    return result


def build_connected_index(project, connection, force=False, extracted_knowledge=None):
    migrate_legacy_editions(project, connection)
    discovered = discover_project_documents(project)
    existing = {
        row["virtual_path"]: row
        for row in connection.execute(
            "SELECT id, virtual_path, size, mtime_ns, content_hash, parser_version FROM documents"
        )
    }
    current_paths = {item["virtual_path"] for item in discovered}
    deleted = 0
    indexed = 0
    skipped = 0
    failures = []
    knowledge_stats = {"knowledgeObjects": 0, "objectTerms": 0, "objectMentions": 0, "claims": 0, "claimEvidence": 0}

    connection.execute("BEGIN IMMEDIATE")
    try:
        set_meta(connection, "build_complete", False)
        for virtual_path, row in existing.items():
            if virtual_path not in current_paths and not connection.execute("SELECT 1 FROM document_editions WHERE document_id=?", (row["id"],)).fetchone():
                delete_document(connection, row["id"])
                deleted += 1

        to_parse = []
        for item in discovered:
            previous = existing.get(item["virtual_path"])
            stat = item["path"].stat()
            unchanged = (
                previous
                and previous["size"] == stat.st_size
                and previous["mtime_ns"] == stat.st_mtime_ns
                and previous["parser_version"] == PARSER_VERSION
            )
            if unchanged and not force:
                register_edition(connection, item, previous["id"])
                skipped += 1
                continue
            to_parse.append(item)
        for item, parsed, parse_error in parse_documents_in_order(to_parse, project, use_cache=not force):
            previous = existing.get(item["virtual_path"])
            if parse_error is not None:
                failures.append({"path": item["virtual_path"], "error": str(parse_error)})
                continue
            connection.execute("SAVEPOINT index_document")
            try:
                index_document(connection, project, item, previous["id"] if previous else None, parsed=parsed)
                connection.execute("RELEASE SAVEPOINT index_document")
                indexed += 1
            except Exception as exc:
                connection.execute("ROLLBACK TO SAVEPOINT index_document")
                connection.execute("RELEASE SAVEPOINT index_document")
                failures.append({"path": item["virtual_path"], "error": str(exc)})

        if not failures:
            if extracted_knowledge is not None:
                knowledge_stats = rebuild_knowledge_model(connection, extracted_knowledge)
            # Immutable editions keep their existing extraction when new files arrive.
        set_meta(connection, "index_version", INDEX_VERSION)
        set_meta(connection, "parser_version", PARSER_VERSION)
        set_meta(connection, "last_build_at", time.time())
        set_meta(connection, "last_build_failures", failures)
        set_meta(connection, "build_complete", True)
        if failures:
            connection.rollback()
            with connection:
                set_meta(connection, "last_attempt_at", time.time())
                set_meta(connection, "last_attempt_failures", failures)
        else:
            connection.commit()
            with connection:
                set_meta(connection, "last_attempt_at", time.time())
                set_meta(connection, "last_attempt_failures", [])
    except Exception:
        connection.rollback()
        raise
    return {
        "ok": not failures,
        "applied": not failures,
        "discovered": len(discovered),
        "indexed": indexed,
        "skipped": skipped,
        "deleted": deleted,
        "failures": failures,
        **knowledge_stats,
        **index_stats(connection),
    }


def discover_project_documents(project):
    items = []
    raw_dir = fs_path(project_raw_dir(project))
    if raw_dir.exists():
        for path in sorted(raw_dir.rglob("*")):
            if (
                path.is_file()
                and not any(part in {".index", "document-history"} for part in path.relative_to(raw_dir).parts)
                and not path.name.startswith("~$")
                and path.suffix.lower() in SUPPORTED_RAW_SUFFIXES
                and ("_sourceRefs" not in project or path.relative_to(raw_dir).as_posix() in project["_sourceRefs"])
            ):
                items.append(
                    {
                        "path": path,
                        "virtual_path": f"raw/sources/{path.relative_to(raw_dir).as_posix()}",
                        "relative_path": path.relative_to(raw_dir).as_posix(),
                        "document_type": "raw",
                    }
                )
    return discover_editions(project, items)


def get_ingestion_documents(project, relative_paths=None):
    """Return the canonical V7 chunks that LLM ingestion must consume."""
    connection = connect_index(project)
    if connection is None:
        return []
    selected = {
        str(value).replace("\\", "/").casefold()
        for value in (relative_paths or [])
        if str(value).strip()
    }
    import_ids = get_meta(connection, "document_import_documents", []) if selected and project.get("_indexPath") else []
    rows = connection.execute(
        """
        SELECT d.id AS document_id, d.relative_path, d.absolute_path, d.title,
               d.content_hash AS document_content_hash,
               c.id AS chunk_id, c.document_version_id, c.structural_node_id,
               c.chunk_no, c.heading_path, c.start_offset, c.end_offset,
               c.content, c.content_hash
        FROM documents d JOIN chunks c ON c.document_id=d.id
        WHERE d.document_type='raw'
        ORDER BY d.relative_path, c.chunk_no
        """
    ).fetchall()
    connection.close()
    documents = {}
    for row in rows:
        if selected and (row["document_id"] not in import_ids if import_ids else row["relative_path"].replace("\\", "/").casefold() not in selected):
            continue
        document = documents.setdefault(row["document_id"], {
            "documentId": row["document_id"],
            "sourceRef": row["relative_path"],
            "path": Path(row["absolute_path"]),
            "title": row["title"],
            "contentHash": row["document_content_hash"],
            "sections": [],
        })
        document["sections"].append({
            "text": row["content"],
            "sectionId": row["structural_node_id"],
            "structuralNodeId": row["structural_node_id"],
            "chunkId": row["chunk_id"],
            "documentVersionId": row["document_version_id"],
            "chunkNo": row["chunk_no"],
            "headingPath": row["heading_path"],
            "startOffset": row["start_offset"],
            "endOffset": row["end_offset"],
            "contentHash": row["content_hash"],
            "charCount": len(row["content"]),
            "summary": "",
        })
    return list(documents.values())


def delete_document(connection, document_id):
    chunk_ids = [
        row["id"]
        for row in connection.execute("SELECT id FROM chunks WHERE document_id = ?", (document_id,))
    ]
    if chunk_ids:
        connection.executemany("DELETE FROM chunk_fts WHERE rowid = ?", [(value,) for value in chunk_ids])
    connection.execute("DELETE FROM documents WHERE id = ?", (document_id,))


def parse_documents_in_order(items, project, use_cache=True, markdown_dir=None):
    """Bounded parallel parsing; the caller alone writes SQLite in stable order."""
    if not items:
        return
    workers = max(1, min(4, int(os.getenv("DOCUMENT_CONVERSION_CONCURRENCY", "2"))))
    total, completed = len(items), 0
    progress_lock = threading.Lock()
    cache_dir = fs_path(project_root(project) / ".index" / "parsed-cache")
    conversion_event("batch_start", project_root(project), total=total, concurrency=workers,
                     converterVersion=CONVERTER_VERSION, parserVersion=PARSER_VERSION)

    def parse(item, ordinal):
        nonlocal completed
        source = item.get("edition", {}).get("source_ref", item["relative_path"])
        record = {"sourceRef": source, "ordinal": ordinal, "total": total}
        conversion_event("file_start", item["path"], **record)
        started = time.perf_counter()
        cached, parsed, error, markdown = False, None, None, None
        try:
            # Snapshot bytes are already hashed during discovery. Hash again here
            # so cache reuse also validates the exact bytes given to the parser.
            digest = hashlib.sha256()
            with item["path"].open("rb") as stream:
                for block in iter(lambda: stream.read(1024 * 1024), b""):
                    digest.update(block)
            key = hashlib.sha256(f"{PARSER_VERSION}|{item['path'].suffix.lower()}|{digest.hexdigest()}".encode()).hexdigest()
            cache = cache_dir / f"{key}.json"
            try:
                data = json.loads(cache.read_text(encoding="utf-8")) if use_cache else None
                value = data.get("parsed") if isinstance(data, dict) else None
                if (isinstance(data, dict) and data.get("parserVersion") == PARSER_VERSION and data.get("sourceHash") == digest.hexdigest()
                    and isinstance(value, dict) and isinstance(value.get("text"), str)
                    and all(isinstance(value.get(k), list) for k in ("sections", "segments", "blocks"))):
                    if markdown_dir is None or isinstance(data.get("markdown"), str):
                        parsed, cached, markdown = value, True, data.get("markdown")
            except (OSError, ValueError, TypeError):
                pass
            if parsed is None:
                if markdown_dir is None:
                    parsed = read_indexable_document(item["path"])
                else:
                    markdown = convert_to_markdown(item["path"])
                    parsed = parse_text_document(markdown, infer_numbered_headings=item["path"].suffix.lower() in {".txt", ".md"})
                temporary = cache_dir / f".write-{uuid.uuid4().hex}.tmp"
                try:
                    cache_dir.mkdir(parents=True, exist_ok=True)
                    temporary.write_text(json.dumps({"parserVersion": PARSER_VERSION, "sourceHash": digest.hexdigest(), "parsed": parsed, "markdown": markdown}, ensure_ascii=False), encoding="utf-8")
                    os.replace(temporary, cache)
                except OSError:
                    conversion_event("cache_write_failed", item["path"], **record)
                finally:
                    try:
                        temporary.unlink(missing_ok=True)
                    except OSError:
                        pass
            if markdown_dir is not None:
                directory = fs_path(markdown_dir)
                directory.mkdir(parents=True, exist_ok=True)
                name = Path(source).stem[:48]
                archive = directory / f"{name}-{hashlib.sha256(item['relative_path'].encode()).hexdigest()[:24]}.md"
                temporary = directory / f".write-{uuid.uuid4().hex}.tmp"
                try:
                    temporary.write_text(markdown, encoding="utf-8")
                    os.replace(temporary, archive)
                finally:
                    temporary.unlink(missing_ok=True)
                item["markdownPath"] = str(archive)
        except Exception as exc:
            error = exc
        with progress_lock:
            completed += 1
            conversion_event("file_failed" if error else "file_done", item["path"], **record,
                current=completed, seconds=round(time.perf_counter() - started, 3), cached=cached,
                outputChars=len(parsed["text"]) if parsed else 0,
                **({"errorType": type(error).__name__, "error": str(error)[:2000]} if error else {}))
        return item, parsed, error

    iterator = iter(enumerate(items, start=1))
    pending = deque()
    executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="wiki-parse")
    try:
        for ordinal, item in iterator:
            pending.append(executor.submit(parse, item, ordinal))
            if len(pending) >= workers * 2:
                yield pending.popleft().result()
        while pending:
            yield pending.popleft().result()
    finally:
        for future in pending:
            future.cancel()
        executor.shutdown(wait=True, cancel_futures=True)


def index_document(connection, project, item, previous_id=None, parsed=None):
    path = item["path"]
    stat = path.stat()
    source_ref = item.get("edition", {}).get("source_ref", item["relative_path"])
    if parsed is None:
        conversion_event("source_start", path, sourceRef=source_ref)
        parsed = read_indexable_document(path)
    text = parsed["text"]
    digest = hashlib.sha256(text.encode("utf-8", errors="ignore")).hexdigest()
    title = Path(item.get("edition", {}).get("source_ref", item["relative_path"])).stem
    document_path = document_structure_path(item.get("edition", {}).get("source_ref", item["relative_path"]))
    values = (
        str(project.get("id") or project_root(project)), item["virtual_path"], item["relative_path"],
        str(path), item["document_type"], title, path.suffix.lower(),
        mimetypes.guess_type(path.name)[0] or "application/octet-stream", stat.st_size,
        stat.st_mtime_ns, digest, PARSER_VERSION, text, time.time(), "indexed",
    )
    if previous_id:
        previous = connection.execute("SELECT content_hash, parser_version FROM documents WHERE id=?", (previous_id,)).fetchone()
        if previous["content_hash"] == digest and previous["parser_version"] == PARSER_VERSION:
            register_edition(connection, item, previous_id)
            return
        clear_document_chunks(connection, previous_id)
        connection.execute(
            """
            UPDATE documents SET
                project_id=?, virtual_path=?, relative_path=?, absolute_path=?, document_type=?, title=?,
                suffix=?, mime_type=?, size=?, mtime_ns=?, content_hash=?, parser_version=?,
                extracted_text=?, indexed_at=?, status=?
            WHERE id=?
            """,
            (*values, previous_id),
        )
        document_id = previous_id
    else:
        cursor = connection.execute(
            """
            INSERT INTO documents(
                project_id, virtual_path, relative_path, absolute_path, document_type, title,
                suffix, mime_type, size, mtime_ns, content_hash, parser_version,
                extracted_text, indexed_at, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            values,
        )
        document_id = cursor.lastrowid
    register_edition(connection, item, document_id)
    connection.execute(
        """
        INSERT OR IGNORE INTO document_versions(document_id, content_hash, parser_version, extracted_text, created_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (document_id, digest, PARSER_VERSION, text, time.time()),
    )
    version_id = connection.execute(
        "SELECT id FROM document_versions WHERE document_id=? AND content_hash=? AND parser_version=?",
        (document_id, digest, PARSER_VERSION),
    ).fetchone()["id"]
    chunks = split_document_chunks(parsed)
    all_domain_terms = [
        row["alias"]
        for row in connection.execute(
            "SELECT DISTINCT term AS alias FROM object_terms ORDER BY length(term) DESC LIMIT 5000"
        )
    ]
    folded_document = text.casefold()
    domain_terms = [term for term in all_domain_terms if str(term).casefold() in folded_document]
    root_node_id = stable_node_id(version_id, "document", 0, 0, len(text), digest)
    connection.execute(
        """
        INSERT OR IGNORE INTO structural_nodes(
            id, document_version_id, parent_id, node_type, ordinal, heading, heading_path,
            page_no, start_offset, end_offset, content_hash
        ) VALUES (?, ?, NULL, 'document', 0, ?, ?, NULL, 0, ?, ?)
        """,
        (root_node_id, version_id, title, document_path, len(text), digest),
    )
    section_node_ids = {}
    for ordinal, section in enumerate(parsed["sections"], start=1):
        content = text[section["start_offset"]:section["end_offset"]]
        content_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
        node_id = stable_node_id(
            version_id, "section", ordinal, section["start_offset"], section["end_offset"], content_hash
        )
        parent_id = section_node_ids.get(section.get("parent_index"), root_node_id)
        connection.execute(
            """
            INSERT OR IGNORE INTO structural_nodes(
                id, document_version_id, parent_id, node_type, ordinal, heading, heading_path,
                page_no, start_offset, end_offset, content_hash
            ) VALUES (?, ?, ?, 'section', ?, ?, ?, NULL, ?, ?, ?)
            """,
            (
                node_id, version_id, parent_id, ordinal, section["heading"],
                join_structure_path(document_path, section["heading_path"]),
                section["start_offset"], section["end_offset"], content_hash,
            ),
        )
        section_node_ids[section["index"]] = node_id

    for ordinal, block in enumerate(parsed["blocks"], start=1):
        content = text[block["start_offset"]:block["end_offset"]]
        if not content.strip():
            continue
        content_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
        node_id = stable_node_id(
            version_id, block["node_type"], ordinal, block["start_offset"], block["end_offset"], content_hash
        )
        connection.execute(
            """
            INSERT OR IGNORE INTO structural_nodes(
                id, document_version_id, parent_id, node_type, ordinal, heading, heading_path,
                page_no, start_offset, end_offset, content_hash
            ) VALUES (?, ?, ?, ?, ?, '', ?, NULL, ?, ?, ?)
            """,
            (
                node_id, version_id, section_node_ids.get(block.get("section_index"), root_node_id),
                block["node_type"], ordinal, join_structure_path(document_path, block["heading_path"]),
                block["start_offset"], block["end_offset"], content_hash,
            ),
        )

    for chunk_no, chunk in enumerate(chunks):
        content = chunk["content"]
        content_hash = hashlib.sha256(content.encode("utf-8", errors="ignore")).hexdigest()
        node_id = section_node_ids.get(chunk.get("section_index"), root_node_id)
        cursor = connection.execute(
            """
            INSERT INTO chunks(
                document_id, document_version_id, structural_node_id, chunk_no, heading_path,
                page_no, start_offset, end_offset, content, content_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                document_id, version_id, node_id, chunk_no,
                join_structure_path(document_path, chunk["heading_path"]), chunk.get("page_no"),
                chunk["start_offset"], chunk["end_offset"], content, content_hash,
            ),
        )
        chunk_id = cursor.lastrowid
        connection.execute(
            "INSERT INTO chunk_fts(rowid, title, relative_path, heading_path, tokens, content) VALUES (?, ?, ?, ?, ?, ?)",
            (
                chunk_id, title, item["relative_path"], join_structure_path(document_path, chunk["heading_path"]),
                index_tokens(
                    " ".join((title, item["relative_path"], document_path, chunk["heading_path"], content)),
                    domain_terms,
                ),
                content,
            ),
        )



def clear_document_chunks(connection, document_id):
    chunk_ids = [row["id"] for row in connection.execute("SELECT id FROM chunks WHERE document_id=?", (document_id,))]
    connection.executemany("DELETE FROM chunk_fts WHERE rowid=?", [(chunk_id,) for chunk_id in chunk_ids])
    connection.execute("DELETE FROM chunks WHERE document_id=?", (document_id,))


def stable_node_id(version_id, node_type, ordinal, start_offset, end_offset, content_hash):
    identity = f"{version_id}:{node_type}:{ordinal}:{start_offset}:{end_offset}:{content_hash}"
    return "nod_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:24]


def document_structure_path(relative_path):
    parts = [part for part in str(relative_path).replace("\\", "/").split("/") if part]
    if len(parts) > 2 and parts[0] == "上传文件" and re.fullmatch(r"[0-9a-f]{64}", parts[1]):
        parts = parts[2:]
    return " > ".join(parts)


def join_structure_path(document_path, heading_path):
    return " > ".join(value for value in (document_path, str(heading_path or "").strip()) if value)


def resolve_indexed_source(source_ref, by_relative, by_basename):
    relative = str(source_ref or "").replace("\\", "/").lstrip("/")
    if relative.startswith("raw/sources/"):
        relative = relative[len("raw/sources/"):]
    exact = by_relative.get(relative.casefold())
    if exact:
        return exact
    candidates = by_basename.get(Path(relative).name.casefold(), [])
    return candidates[0] if len(candidates) == 1 else None


def rebuild_knowledge_model(connection, payload, merge=False):
    if not merge:
        for table in ("object_version_profiles", "claim_evidence", "claims", "object_mentions", "object_sources", "object_terms", "knowledge_objects"):
            connection.execute(f"DELETE FROM {table}")
    objects = [item for item in (payload or {}).get("objects", []) if isinstance(item, dict)]
    object_by_path = {}
    object_by_name = {row["normalized_name"]: row["id"] for row in connection.execute("SELECT id, normalized_name FROM knowledge_objects")}
    for item in objects:
        name = str(item.get("name") or item.get("canonicalName") or "").strip()
        normalized = canonical_key(name)
        if not normalized:
            continue
        source_type = str(item.get("type") or "")
        object_key = str(item.get("objectId") or "").strip() or (
            "obj_" + hashlib.sha256(f"{source_type}:{normalized}".encode("utf-8")).hexdigest()[:20]
        )
        description = str(item.get("description") or "").strip()
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO knowledge_objects(
                object_key, canonical_name, normalized_name, domain_type,
                ontology_kind, description, status
            ) VALUES (?, ?, ?, ?, ?, ?, 'candidate')
            """,
            (
                object_key, name, normalized,
                normalized_domain_type(item.get("domainType"), source_type),
                normalized_ontology_kind(item.get("ontologyKind"), source_type), description,
            ),
        )
        object_id = connection.execute("SELECT id FROM knowledge_objects WHERE object_key=?", (object_key,)).fetchone()[0]
        object_by_name[normalized] = object_id
        wiki_path = str(item.get("wikiPath") or "").replace("\\", "/").casefold()
        if wiki_path:
            object_by_path[wiki_path] = object_id
        terms = [
            (name, "canonical"),
            *[(value, "alias") for value in item.get("aliases") or []],
            *[(value, "keyword") for value in item.get("keywords") or []],
        ]
        for term, kind in terms:
            term = str(term or "").strip()
            normalized_term = canonical_key(term)
            if normalized_term:
                connection.execute(
                    "INSERT OR IGNORE INTO object_terms(object_id, term, normalized_term, term_kind, provenance) VALUES (?, ?, ?, ?, 'extracted')",
                    (object_id, term, normalized_term, kind),
                )

    raw_rows = connection.execute(
        "SELECT id, relative_path FROM documents WHERE document_type = 'raw'"
    ).fetchall()
    raw_by_relative = {row["relative_path"].replace("\\", "/").casefold(): row for row in raw_rows}
    raw_by_basename = {}
    for row in raw_rows:
        raw_by_basename.setdefault(Path(row["relative_path"]).name.casefold(), []).append(row)

    for item in objects:
        name = str(item.get("name") or item.get("canonicalName") or "").strip()
        subject_id = object_by_name.get(canonical_key(name))
        if not subject_id:
            continue
        source_refs = item.get("sources") or []
        if isinstance(source_refs, str):
            source_refs = [source_refs]
        for source_ref in source_refs:
            document = resolve_indexed_source(source_ref, raw_by_relative, raw_by_basename)
            if document:
                connection.execute(
                    "INSERT OR IGNORE INTO object_sources(object_id, document_id, source_ref) VALUES (?, ?, ?)",
                    (subject_id, document["id"], str(source_ref)),
                )
        for mention in item.get("mentions") or []:
            if not isinstance(mention, dict):
                continue
            evidence = direct_chunk_evidence(connection, mention)
            if not evidence:
                continue
            connection.execute(
                "INSERT OR IGNORE INTO object_sources(object_id, document_id, source_ref) VALUES (?, ?, ?)",
                (subject_id, evidence["document_id"], str(mention.get("sourceRelativePath") or evidence["relative_path"])),
            )
            mention_id = str(mention.get("mentionId") or "").strip() or (
                "men_" + hashlib.sha256(
                    f"{subject_id}:{evidence['chunk_id']}:{mention.get('evidence') or name}".encode("utf-8")
                ).hexdigest()[:20]
            )
            connection.execute(
                """
                INSERT OR REPLACE INTO object_mentions(
                    mention_id, object_id, document_version_id, structural_node_id, raw_chunk_id,
                    start_offset, end_offset, matched_text, evidence, extraction_summary, confidence
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    mention_id, subject_id, evidence["document_version_id"], evidence["structural_node_id"],
                    evidence["chunk_id"], evidence["start_offset"], evidence["end_offset"], name,
                    str(mention.get("evidence") or evidence["quoted_text"]),
                    str(mention.get("summary") or ""), 1.0,
                ),
            )

        record_profiles(connection, subject_id, item)
        for relation in item.get("relations") or []:
            if not isinstance(relation, dict):
                continue
            predicate = str(relation.get("type") or relation.get("relation") or "").strip()
            target_ref = str(relation.get("target") or "").strip().replace("\\", "/")
            literal_value = str(relation.get("value") or "").strip()
            target_id = object_by_path.get(target_ref.casefold())
            if target_id is None and target_ref:
                target_id = object_by_name.get(canonical_key(Path(target_ref).stem)) or object_by_name.get(canonical_key(target_ref))
            if not predicate or (target_id is None and not literal_value):
                continue
            qualifiers = relation.get("qualifiers") or relation.get("conditions") or {}
            if not isinstance(qualifiers, dict):
                qualifiers = {"note": str(qualifiers)}
            qualifiers_json = json.dumps(qualifiers, ensure_ascii=False, sort_keys=True)
            identity = "\n".join((
                canonical_key(name), predicate.casefold(), str(target_id or literal_value.casefold()), qualifiers_json,
            ))
            claim_id = "clm_" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]
            connection.execute(
                """
                INSERT OR IGNORE INTO claims(
                    claim_id, subject_object_id, predicate, object_object_id,
                    literal_value, qualifiers_json, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (claim_id, subject_id, predicate, target_id, literal_value, qualifiers_json, "candidate"),
            )
            for pointer in relation.get("evidence") or []:
                evidence = direct_chunk_evidence(connection, pointer)
                if evidence:
                    connection.execute(
                        """
                        INSERT OR REPLACE INTO claim_evidence(
                            claim_id, document_id, document_version_id, structural_node_id, chunk_id,
                            start_offset, end_offset, quoted_text, score
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (
                            claim_id, evidence["document_id"], evidence["document_version_id"],
                            evidence["structural_node_id"], evidence["chunk_id"], evidence["start_offset"],
                            evidence["end_offset"], evidence["quoted_text"], evidence["score"],
                        ),
                    )

    return {
        "knowledgeObjects": table_count(connection, "knowledge_objects"),
        "objectTerms": table_count(connection, "object_terms"),
        "objectMentions": table_count(connection, "object_mentions"),
        "claims": table_count(connection, "claims"),
        "claimEvidence": table_count(connection, "claim_evidence"),
    }


def normalized_domain_type(value, source_type=""):
    allowed = {
        "organization", "role", "system", "product", "document", "interface", "message",
        "event", "process", "rule", "condition", "metric", "term", "other",
    }
    value = str(value or "").casefold()
    if value in allowed:
        return value
    return "term" if str(source_type or "").casefold() == "concept" else "other"


def normalized_ontology_kind(value, source_type=""):
    value = str(value or "").casefold()
    if value in {"individual", "class", "concept", "unspecified"}:
        return value
    if str(source_type or "").casefold() == "concept":
        return "concept"
    if str(source_type or "").casefold() == "entity":
        return "individual"
    return "unspecified"


def direct_chunk_evidence(connection, pointer):
    try:
        chunk_id = int(pointer.get("chunkId"))
    except (AttributeError, TypeError, ValueError):
        return None
    row = connection.execute(
        """
        SELECT c.id AS chunk_id, c.document_id, c.document_version_id, c.structural_node_id,
               c.start_offset AS chunk_start, c.end_offset AS chunk_end, c.content,
               d.relative_path
        FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id=?
        """,
        (chunk_id,),
    ).fetchone()
    if not row:
        return None
    expected_version = pointer.get("documentVersionId")
    expected_node = pointer.get("structuralNodeId")
    if expected_version is not None:
        try:
            if int(expected_version) != row["document_version_id"]:
                return None
        except (TypeError, ValueError):
            return None
    if expected_node and expected_node != row["structural_node_id"]:
        return None
    start = max(row["chunk_start"], int(pointer.get("startOffset") or row["chunk_start"]))
    end = min(row["chunk_end"], int(pointer.get("endOffset") or row["chunk_end"]))
    if end <= start:
        start, end = row["chunk_start"], row["chunk_end"]
    return {
        **dict(row),
        "start_offset": start,
        "end_offset": end,
        "quoted_text": row["content"],
        "score": 1.0,
    }


def read_indexable_document(path):
    return parse_text_document(convert_to_markdown(path), infer_numbered_headings=path.suffix.lower() in {".txt", ".md"})


def parse_text_document(text, infer_numbered_headings=True):
    blocks = []
    paragraph = []
    block_type = "paragraph"
    def flush():
        if paragraph:
            blocks.append({"node_type": block_type, "text": "\n".join(paragraph).strip()})
            paragraph.clear()
    for line in str(text or "").replace("\r\n", "\n").split("\n"):
        markdown = re.match(r"^(#{1,6})\s+(.+)$", line.strip())
        numbered = re.match(r"^(第[一二三四五六七八九十百千万0-9]+[章节篇].{0,80}|[0-9]+(?:\.[0-9]+)*[.、]?\s*[^。；]{1,80})$", line.strip())
        if markdown or (infer_numbered_headings and numbered):
            flush()
            if markdown:
                level, heading = len(markdown.group(1)), markdown.group(2).strip()
            else:
                number = re.match(r"([0-9]+(?:\.[0-9]+)*)", line.strip())
                level = min(6, len(number.group(1).split("."))) if number else 1
                heading = line.strip()
            blocks.append({"node_type": "section", "text": heading, "heading_level": level})
        elif line.strip():
            kind = "table" if line.lstrip().startswith("|") else "list" if re.match(r"^\s*(?:[-+*]|\d+[.)])\s+", line) else "paragraph"
            if kind != block_type:
                flush()
                block_type = kind
            paragraph.append(line)
        else:
            flush()
    flush()
    return build_parsed_document(blocks)


def build_parsed_document(source_blocks):
    source_blocks = strip_front_matter_blocks(source_blocks)
    parts = []
    sections = []
    blocks = []
    segments = []
    stack = []
    offset = 0

    def append(text):
        nonlocal offset
        if parts:
            parts.append("\n\n")
            offset += 2
        start = offset
        parts.append(text)
        offset += len(text)
        return start, offset

    for source in source_blocks:
        if source["node_type"] == "section":
            level = int(source["heading_level"])
            while stack and sections[stack[-1]]["level"] >= level:
                sections[stack.pop()]["end_offset"] = offset
            parent_index = stack[-1] if stack else None
            heading_path = " > ".join([*(sections[index]["heading"] for index in stack), source["text"]])
            start, end = append(f"{'#' * min(level, 6)} {source['text']}")
            index = len(sections)
            sections.append({
                "index": index, "parent_index": parent_index, "level": level,
                "heading": source["text"], "heading_path": heading_path,
                "start_offset": start, "end_offset": end,
            })
            stack.append(index)
            segments.append({
                "section_index": index, "heading_path": heading_path,
                "start_offset": start, "end_offset": end,
            })
            continue
        text = source["text"].strip()
        if not text:
            continue
        start, end = append(text)
        section_index = stack[-1] if stack else None
        heading_path = sections[section_index]["heading_path"] if section_index is not None else ""
        blocks.append({
            "node_type": source["node_type"], "section_index": section_index,
            "heading_path": heading_path, "start_offset": start, "end_offset": end,
        })
        if segments and segments[-1]["section_index"] == section_index:
            segments[-1]["end_offset"] = end
        else:
            segments.append({
                "section_index": section_index, "heading_path": heading_path,
                "start_offset": start, "end_offset": end,
            })
    while stack:
        sections[stack.pop()]["end_offset"] = offset
    return {"text": "".join(parts), "sections": sections, "blocks": blocks, "segments": segments}


def strip_front_matter_blocks(source_blocks):
    noise_headings = {
        "目录", "目次", "修订记录", "历史修订记录", "文档修订记录",
        "版本历史", "版本记录", "变更记录", "更新记录",
    }
    kept = []
    skipping = False
    for block in source_blocks:
        if block["node_type"] == "section":
            heading = re.sub(r"[\s：:、，,。；;（）()\[\]【】]", "", str(block.get("text") or ""))
            skipping = heading in noise_headings
            if skipping:
                continue
        if not skipping:
            kept.append(block)
    return kept


def split_document_chunks(parsed, target_chars=1200, overlap_chars=160):
    text = parsed["text"]
    chunks = []
    for segment in parsed["segments"]:
        start = segment["start_offset"]
        end = segment["end_offset"]
        while start < end:
            cut = min(end, start + target_chars)
            if cut < end:
                boundary = text.rfind("\n\n", start + target_chars // 2, cut)
                if boundary > start:
                    cut = boundary
            content_start = start
            content_end = cut
            while content_start < content_end and text[content_start].isspace():
                content_start += 1
            while content_end > content_start and text[content_end - 1].isspace():
                content_end -= 1
            if content_start < content_end:
                chunks.append({
                    "section_index": segment["section_index"],
                    "heading_path": segment["heading_path"], "page_no": None,
                    "start_offset": content_start, "end_offset": content_end,
                    "content": text[content_start:content_end],
                })
            if cut >= end:
                break
            start = max(start + 1, cut - overlap_chars)
    return chunks


def canonical_key(value):
    text = str(value or "").strip().casefold()
    return re.sub(r"[《》\"'“”‘’\s_\-—/\\（）()，,。；;：:、？！!?]+", "", text)


def index_tokens(value, domain_terms=None):
    tokens = []
    seen = set()
    folded_value = str(value or "").casefold()
    for term in domain_terms or []:
        clean = str(term or "").strip().casefold()
        if len(clean) >= 2 and clean in folded_value and clean not in seen:
            seen.add(clean)
            tokens.append(clean)
    for run in re.findall(r"[\u4e00-\u9fff]+|[A-Za-z0-9_\-]+", folded_value):
        candidates = [run]
        if re.fullmatch(r"[\u4e00-\u9fff]+", run):
            for width in (2, 3):
                candidates.extend(run[index:index + width] for index in range(max(0, len(run) - width + 1)))
        for token in candidates:
            if token and token not in seen:
                seen.add(token)
                tokens.append(token)
    return " ".join(tokens)


def query_terms(query):
    query = str(query or "").strip()
    normalized = query.strip(" \t\r\n?？!！。.")
    cleaned = re.sub(r"^(什么是|何为|查询|检索|解释|说明|关于|请问)", "", normalized).strip()
    cleaned = re.sub(
        r"(是什么|是啥|是什么意思|有哪些|如何|怎么|为何|为什么|的作用|的要求|的规则|的流程|的条件)$",
        "",
        cleaned,
    ).strip()
    values = [cleaned, normalized]
    values.extend(re.split(r"[\s,，。？?！!；;：:、()（）\[\]【】《》<>\"']+", normalized))
    terms = []
    seen = set()
    for value in values:
        value = value.strip()
        key = value.casefold()
        if len(value) < 2 or key in seen:
            continue
        seen.add(key)
        terms.append(value)
    return terms[:20]


def search_project_index(project, query, limit=8, roles=None):
    from .query_roles import query_roles, contains_term, meaningful_phrases, valid_phrase
    roles = roles if roles is not None else query_roles(query)
    if not roles.get("_domainTokenized"):
        dictionary_connection = connect_index(scope_project(project, project.get("_versionScope")))
        domain_terms = []
        if dictionary_connection is not None:
            try:
                domain_terms = [row[0] for row in dictionary_connection.execute(
                    "SELECT DISTINCT term FROM object_terms WHERE term_kind IN ('canonical', 'alias') ORDER BY term"
                )]
            finally:
                dictionary_connection.close()
        roles = {**query_roles(roles.get("originalQuestion") or query, roles, domain_terms), "_domainTokenized": True}
    token_fallback = roles.get("tokenFallback")
    if token_fallback:
        full_roles = {**roles, "tokenFallback": None, "coreObjects": [token_fallback["phrase"]],
                      "phrases": [token_fallback["phrase"]], "coreConfidence": "high"}
        exact = search_project_index(project, token_fallback["phrase"], limit, full_roles)
        required = token_fallback["terms"]
        if len(exact["results"]) < limit and exact.get("index", {}).get("ok"):
            split_roles = {**roles, "tokenFallback": None, "coreObjects": required,
                           "phrases": required, "coreConfidence": "high"}
            fallback = search_project_index(project, " ".join(required), max(limit, 20), split_roles)
            seen = {item["chunkId"] for item in exact["results"]}
            for item in fallback["results"]:
                if item["chunkId"] in seen or not set(required).issubset(item["retrieval"]["coreHits"]):
                    continue
                item["retrieval"]["reason"] = "完整短语结果不足；jieba 实词联合命中：" + "、".join(required)
                exact["results"].append(item)
                seen.add(item["chunkId"])
            exact["results"] = exact["results"][:limit]
            exact["index"]["tokenFallbackUsed"] = True
            exact["index"]["elapsedMs"] = exact["index"].get("elapsedMs", 0) + fallback["index"].get("elapsedMs", 0)
        exact["index"]["queryRoles"] = roles
        return exact
    compound = roles.get("compoundFallback")
    if compound:
        # Run the existing validated phrase path first. Attribute words never
        # become independent fallback recall terms.
        full_roles = {**roles, "compoundFallback": None, "coreObjects": [compound["phrase"]],
                      "phrases": [compound["phrase"]], "requestedAttribute": "", "coreConfidence": "high"}
        exact = search_project_index(project, compound["phrase"], limit, full_roles)
        exact_items = [item for item in exact["results"] if any(
            contains_term(item.get(field, ""), compound["phrase"]) for field in ("snippet", "headingPath", "title", "documentPath")
        ) or compound["phrase"] in item.get("retrieval", {}).get("coreMatchedTerms", [])]
        fallback_used = len(exact_items) < limit and exact.get("index", {}).get("ok", False)
        background = {"results": [], "index": {}}
        if fallback_used:
            fallback_roles = {**roles, "compoundFallback": None, "coreObjects": [compound["object"]],
                              "phrases": [compound["object"]], "coreConfidence": "high"}
            background = search_project_index(project, compound["object"], limit, fallback_roles)
        merged, seen = [], set()
        for item in [*exact_items, *background["results"]]:
            if item["chunkId"] in seen:
                continue
            if item not in exact_items and compound["object"] not in item.get("retrieval", {}).get("coreHits", []):
                continue
            seen.add(item["chunkId"])
            exact_hit = item in exact_items
            item["retrieval"].update({
                "coreObjects": roles["coreObjects"], "requestedAttribute": compound["attribute"],
                "phraseFallback": not exact_hit,
                "reason": (f"完整短语「{compound['phrase']}」优先命中；" if exact_hit else
                           f"完整短语结果不足，回退到核心词「{compound['object']}」；「{compound['attribute']}」仅作辅助排序；") + item["retrieval"]["reason"],
            })
            merged.append(item)
            if len(merged) >= limit:
                break
        metadata = {**exact.get("index", {}), "queryRoles": roles, "phraseFallbackUsed": fallback_used,
                    "exactPhraseResults": len(exact_items)}
        metadata["coreVerified"] = any(item.get("retrieval", {}).get("coreHits") for item in merged)
        metadata["globalFtsUsed"] = bool(metadata.get("globalFtsUsed") or background.get("index", {}).get("globalFtsUsed"))
        metadata["elapsedMs"] = exact.get("index", {}).get("elapsedMs", 0) + background.get("index", {}).get("elapsedMs", 0)
        return {"results": merged, "index": metadata}
    core_objects = roles.get("coreObjects", [])
    scope_terms = roles.get("scopeTerms", [])
    started = time.perf_counter()
    connection = connect_index(scope_project(project, project.get("_versionScope")))
    if connection is None:
        return {
            "results": [],
            "index": {"ok": False, "reason": "索引不存在，请先构建项目搜索索引"},
        }
    if get_meta(connection, "index_version") != INDEX_VERSION or not get_meta(connection, "build_complete", False):
        connection.close()
        return {
            "results": [],
            "index": {"ok": False, "reason": "索引版本过旧或构建未完成，请重建项目搜索索引"},
        }
    build_info = None

    core_variants = {}
    for core in core_objects:
        aliases = connection.execute(
            "SELECT DISTINCT t.term FROM object_terms t JOIN object_terms matched ON matched.object_id=t.object_id "
            "WHERE matched.normalized_term=? AND matched.term_kind IN ('canonical','alias') "
            "AND t.term_kind IN ('canonical','alias') LIMIT 16", (canonical_key(core),)
        ).fetchall()
        core_variants[core] = list(dict.fromkeys([core, *[row[0] for row in aliases]]))
    terms = list(dict.fromkeys([*core_objects, *(roles.get("phrases") or meaningful_phrases(query))]))
    normalized_query = canonical_key(roles.get("originalQuestion") or query)
    object_candidates = []
    for row in connection.execute(
        """
        SELECT k.id AS object_id, k.canonical_name, k.normalized_name,
               k.domain_type,
               t.term AS alias, t.normalized_term AS normalized_alias
        FROM object_terms t JOIN knowledge_objects k ON k.id=t.object_id
        ORDER BY length(t.normalized_term) DESC
        """
    ):
        alias = row["normalized_alias"]
        if valid_phrase(row["alias"]) and any(contains_term(term, row["alias"]) for term in terms):
            object_candidates.append(row)

    object_matches = []
    occupied = []
    for row in object_candidates:
        alias = row["normalized_alias"]
        start = normalized_query.find(alias)
        end = start + len(alias)
        if any(start < used_end and end > used_start for used_start, used_end in occupied):
            continue
        occupied.append((start, end))
        object_matches.append(row)

    if not object_matches:
        object_matches = description_object_matches(connection, query, max(limit, 8), terms)
    terms = list(dict.fromkeys([*terms, *[row["alias"] for row in object_matches if row["normalized_alias"]]]))

    candidate_ids = set()
    matched_object_ids = list(dict.fromkeys(row["object_id"] for row in object_matches))
    matched_object_names = {
        row["object_id"]: row["canonical_name"] for row in object_matches
    }
    description_matches = {row["object_id"]: row.get("descriptionTerms", []) for row in object_matches if isinstance(row, dict) and row.get("descriptionTerms")}
    linked_scores = {}
    linked_kinds = {}
    linked_source_document_ids = set()
    if matched_object_ids:
        placeholders = ",".join("?" for _ in matched_object_ids)
        for row in connection.execute(
            f"""
            SELECT m.raw_chunk_id AS chunk_id, m.confidence AS score,
                   m.matched_text, m.object_id, c.document_id
            FROM object_mentions m
            JOIN chunks c ON c.id = m.raw_chunk_id
            JOIN documents d ON d.id = c.document_id
            WHERE m.object_id IN ({placeholders}) AND m.raw_chunk_id IS NOT NULL
              AND d.document_type = 'raw'
            ORDER BY m.confidence DESC
            """,
            tuple(matched_object_ids),
        ):
            candidate_ids.add(row["chunk_id"])
            linked_source_document_ids.add(row["document_id"])
            structured_score = 450 if row["object_id"] in description_matches else max(1250, float(row["score"] or 0))
            if structured_score > linked_scores.get(row["chunk_id"], 0):
                linked_scores[row["chunk_id"]] = structured_score
                linked_kinds[row["chunk_id"]] = {
                    "kind": "structured", "matchedText": row["matched_text"],
                    "objectId": row["object_id"],
                }
        for row in connection.execute(
            f"""
            SELECT c.id AS chunk_id, c.document_id, c.content,
                   t.object_id, t.term AS matched_text, t.term_kind
            FROM object_terms t
            JOIN object_sources s ON s.object_id=t.object_id
            JOIN chunks c ON c.document_id=s.document_id
            WHERE t.object_id IN ({placeholders}) AND t.term_kind IN ('canonical', 'alias')
            """,
            tuple(matched_object_ids),
        ):
            if row["object_id"] in description_matches:
                continue
            if str(row["matched_text"]).casefold() not in row["content"].casefold():
                continue
            candidate_ids.add(row["chunk_id"])
            linked_source_document_ids.add(row["document_id"])
            name_score = 700 if row["term_kind"] == "canonical" else 650
            if name_score > linked_scores.get(row["chunk_id"], 0):
                linked_scores[row["chunk_id"]] = name_score
                linked_kinds[row["chunk_id"]] = {
                    "kind": "name",
                    "matchedText": row["matched_text"],
                    "objectId": row["object_id"],
                }
        for row in connection.execute(
            f"SELECT DISTINCT document_id FROM object_sources WHERE object_id IN ({placeholders})",
            tuple(matched_object_ids),
        ):
            linked_source_document_ids.add(row["document_id"])

    strong_fts_terms = terms
    fallback_fts_terms = []
    fts_scores = {}
    core_candidate_ids = set()
    # Each object gets its own recall budget, so expansion terms cannot crowd it out.
    for core in dict.fromkeys(term for variants in core_variants.values() for term in variants):
        tokens = index_tokens(core).split()
        tokens = [token for token in tokens if len(token) == 3] if re.fullmatch(r"[\u4e00-\u9fff]{3,}", core) else [core.casefold()]
        expression = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
        for hit in connection.execute(
            "SELECT chunk_fts.rowid FROM chunk_fts JOIN chunks c ON c.id=chunk_fts.rowid WHERE chunk_fts MATCH ? ORDER BY bm25(chunk_fts) LIMIT ?",
            (expression, max(200, limit * 30)),
        ):
            core_candidate_ids.add(hit[0])
    candidate_ids.update(core_candidate_ids)
    def add_fts_candidates(tokens, candidate_limit, document_ids=None):
        if not tokens:
            return set()
        added = set()
        try:
            for phrase in tokens:
                if not valid_phrase(phrase):
                    continue
                grams = [token for token in index_tokens(phrase).split() if len(token) == 3] if re.fullmatch(r"[\u4e00-\u9fff]{3,}", phrase) else [phrase]
                expression = " AND ".join('"' + token.replace('"', '""') + '"' for token in grams)
                restriction = f" AND c.document_id IN ({','.join('?' for _ in document_ids)})" if document_ids else ""
                rows = connection.execute(
                    "SELECT c.id AS rowid, c.content, c.heading_path, d.title, d.relative_path, bm25(chunk_fts) AS rank "
                    "FROM chunk_fts JOIN chunks c ON c.id=chunk_fts.rowid JOIN documents d ON d.id=c.document_id "
                    "WHERE chunk_fts MATCH ?" + restriction + " ORDER BY rank LIMIT ?",
                    (expression, *(document_ids or []), candidate_limit),
                )
                for row in rows:
                    if not any(contains_term(row[field], phrase) for field in ("content", "heading_path", "title", "relative_path")):
                        continue
                    candidate_ids.add(row["rowid"])
                    added.add(row["rowid"])
                    score = -float(row["rank"] or 0)
                    fts_scores[row["rowid"]] = max(score, fts_scores.get(row["rowid"], 0))
        except sqlite3.OperationalError:
            pass
        return added

    residual_terms = object_residual_terms(terms, object_matches)
    strong_linked_chunks = {
        chunk_id for chunk_id, link in linked_kinds.items() if link["kind"] == "structured"
    }
    global_fts_used = False
    fallback_reason = ""
    retrieval_mode = "object-linked" if strong_linked_chunks else "global-fts"
    local_fts_ids = set()
    local_tokens = []
    if matched_object_ids and linked_source_document_ids and residual_terms:
        for term in residual_terms:
            local_tokens.extend(meaningful_phrases(term))
        local_tokens = list(dict.fromkeys(local_tokens))[:60]
        local_fts_ids = add_fts_candidates(
            local_tokens, max(limit * 12, 40), sorted(linked_source_document_ids)
        )
        if local_fts_ids:
            retrieval_mode = "object-source-local-fts"

    evidence_sufficient = bool(strong_linked_chunks) and (not residual_terms or bool(local_fts_ids))
    if not evidence_sufficient:
        global_fts_used = True
        retrieval_mode = "global-fts"
        if not object_matches:
            fallback_reason = "未精确命中知识对象"
        elif not linked_source_document_ids:
            fallback_reason = "知识对象没有可解析的来源文档"
        elif not strong_linked_chunks:
            fallback_reason = "知识对象没有直接关联的标准原文 Chunk"
        else:
            fallback_reason = "关联原文证据未覆盖问题中的额外限定词"
        add_fts_candidates(strong_fts_terms, max(limit * 20, 80))
        if len(candidate_ids) < limit:
            add_fts_candidates(fallback_fts_terms, max(limit * 30, 120))

    if not candidate_ids:
        connection.close()
        return {
            "results": [],
            "index": {
                "ok": True,
                "terms": terms,
                "build": build_info,
                "candidateCount": 0,
                "queryRoles": roles,
                "coreVerified": False,
                "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
                "retrievalMode": retrieval_mode,
                "globalFtsUsed": global_fts_used,
                "objectLinkedChunks": len(strong_linked_chunks),
                "linkedSourceDocuments": len(linked_source_document_ids),
                "fallbackReason": fallback_reason,
            },
        }

    placeholders = ",".join("?" for _ in candidate_ids)
    rows = connection.execute(
        f"""
        SELECT c.id AS chunk_id, c.document_version_id, c.structural_node_id,
               c.content, c.heading_path, c.page_no, c.chunk_no, c.start_offset, c.end_offset,
               d.id AS document_id, d.virtual_path, d.relative_path, d.absolute_path, d.document_type, d.title
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.id IN ({placeholders})
        """,
        tuple(candidate_ids),
    ).fetchall()
    scored = []
    for row in rows:
        title_folded = row["title"].casefold()
        content_folded = row["content"].casefold()
        heading_folded = row["heading_path"].casefold()
        path_folded = row["relative_path"].casefold()
        score = linked_scores.get(row["chunk_id"], 0) + fts_scores.get(row["chunk_id"], 0) * 10
        match_kind = "fulltext"
        matched_terms = []
        for index, term in enumerate(terms):
            folded = term.casefold()
            if not folded:
                continue
            if title_folded == folded:
                score += 700 - index * 5
                match_kind = "exact-title" if match_kind == "fulltext" else match_kind
                matched_terms.append(term)
            elif contains_term(row["title"], term):
                score += 320 - index * 3
                matched_terms.append(term)
            if contains_term(row["heading_path"], term):
                score += 200 - index * 2
                if heading_folded.split(" > ")[-1] == folded:
                    score += 700
                matched_terms.append(term)
            if contains_term(row["relative_path"], term):
                score += 160 - index * 2
                matched_terms.append(term)
            occurrences = content_folded.count(folded) if contains_term(row["content"], term) else 0
            if occurrences:
                score += min(560, 500 + occurrences * 10) if len(term) >= 4 else min(50, occurrences * 5)
                if match_kind == "fulltext" and len(term) >= 4:
                    match_kind = "exact-phrase"
                matched_terms.append(term)
        scored.append((score, row, list(dict.fromkeys(matched_terms))))

    core_matches = {}
    scope_matches = {}
    for _, row, _ in scored:
        core_matches[row["chunk_id"]] = [term for term in core_objects if any(contains_term(row["content"], variant) for variant in core_variants[term])]
        scope_text = " ".join((row["heading_path"], row["relative_path"], row["title"], row["content"]))
        scope_matches[row["chunk_id"]] = [term for term in scope_terms if contains_term(scope_text, term)
            or (term.endswith("主机") and len(term) > 2 and contains_term(scope_text, term[:-2]))]
    def priority(item):
        score, row, _ = item
        core = core_matches[row["chunk_id"]]
        scope = scope_matches[row["chunk_id"]]
        tier = (2 if len(scope) == len(scope_terms) else 1) if core else 0
        return (tier, len(core), len(scope) if core else 0,
                sum(contains_term(row["content"], term) for term in core),
                bool(roles.get("requestedAttribute") and contains_term(row["content"], roles["requestedAttribute"])) if core else False,
                bool(row["heading_path"].count(" > ")), score)
    scored.sort(key=priority, reverse=True)
    if len(core_objects) > 1:
        # Reserve one result per comparison object before repeated hits consume the limit.
        selected = []
        covered = set()
        for core in core_objects:
            if core in covered:
                continue
            best = next((item for item in scored if core in core_matches[item[1]["chunk_id"]]), None)
            if best:
                selected.append(best)
                covered.update(core_matches[best[1]["chunk_id"]])
        chosen_ids = {item[1]["chunk_id"] for item in selected}
        scored = selected + [item for item in scored if item[1]["chunk_id"] not in chosen_ids]
    verified_core = any(core_matches.values())
    has_strong_text_match = any(matched_terms for _, _, matched_terms in scored)
    results = []
    path_counts = {}
    for score, row, matched_terms in scored:
        linked = linked_kinds.get(row["chunk_id"])
        core_hits = core_matches[row["chunk_id"]]
        if not linked and not matched_terms and not core_hits:
            continue
        if verified_core and roles.get("coreConfidence") == "high" and not core_hits:
            continue
        if has_strong_text_match and linked and linked["kind"] == "structured" and not matched_terms and not core_hits:
            continue
        if path_counts.get(row["virtual_path"], 0) >= 3:
            continue
        path_counts[row["virtual_path"]] = path_counts.get(row["virtual_path"], 0) + 1
        actual_core_terms = list(dict.fromkeys(variant for core in core_hits for variant in core_variants[core] if contains_term(row["content"], variant)))
        locations = []
        for term in dict.fromkeys([*actual_core_terms, *matched_terms]):
            for field, label in (("content", "正文"), ("heading_path", "章节标题"), ("title", "文件名"), ("relative_path", "文件路径")):
                if contains_term(row[field], term):
                    locations.append({"term": term, "location": label})
        body_terms = list(dict.fromkeys(hit["term"] for hit in locations if hit["location"] == "正文"))
        snippet = relevant_snippet(row["content"], body_terms)
        match_kind = match_kind_for(row["chunk_id"], matched_terms, row, linked_kinds)
        object_names = []
        if linked and linked.get("objectId") in matched_object_names:
            object_names = [matched_object_names[linked["objectId"]]]
        elif row["chunk_id"] in local_fts_ids:
            object_names = list(dict.fromkeys(matched_object_names.values()))
        if match_kind == "object-source-structured":
            chain = [f"知识对象「{object_names[0]}」" if object_names else "知识对象", "抽取 Mention", "当前原文 Chunk"]
            mention_text = str(linked.get("matchedText") or "").strip() if linked else ""
            reason = f"对象抽取记录「{mention_text}」直接指向该原文块"
            if linked.get("objectId") in description_matches:
                matched_description = description_matches[linked["objectId"]]
                reason = f"对象描述完整命中「{'、'.join(matched_description)}」，沿抽取记录关联原文（间接证据）"
                chain.insert(0, "对象描述完整短语匹配")
        elif match_kind == "object-name-match":
            chain = [f"知识对象「{object_names[0]}」" if object_names else "知识对象", "关联来源文档", f"名称「{linked['matchedText']}」命中当前 Chunk"]
            reason = "知识对象名称或别名在关联原文中连续出现"
        elif row["chunk_id"] in local_fts_ids:
            chain = [f"知识对象「{'、'.join(object_names)}」", "关联来源文档", "限定词全文检索", "当前 Chunk"]
            reason = "先按知识对象限定来源文档，再用问题中的额外词召回"
        else:
            chain = ["全项目全文索引", "当前 Chunk"]
            reason = "对象关联证据不足，使用全文索引召回" if global_fts_used else "全文索引命中"
        if core_hits:
            reason = "正文完整命中核心对象「" + "、".join(actual_core_terms) + "」；" + ("范围限定符合" if len(scope_matches[row["chunk_id"]]) == len(scope_terms) else "范围限定待确认")
            if linked:
                chain = [*chain, "正文连续匹配验证", "章节路径与文档范围验证"]
            else:
                chain = ["核心对象独立检索", "正文连续匹配验证", "章节路径与文档范围验证", "当前 Chunk"]
        elif core_objects:
            reason = "未命中核心对象，仅作为相关背景；" + reason
        results.append(
            {
                "path": row["virtual_path"],
                "title": row["title"],
                "score": round(score, 4),
                "snippet": snippet,
                "evidenceSnippet": snippet,
                "evidenceKind": "source",
                "evidenceSourcePath": row["absolute_path"],
                "chunkId": row["chunk_id"],
                "documentId": row["document_id"],
                **edition_metadata(connection, row["document_id"]),
                "documentVersionId": row["document_version_id"],
                "structuralNodeId": row["structural_node_id"],
                "chunkNo": row["chunk_no"],
                "pageNo": row["page_no"],
                "documentPath": row["relative_path"],
                "headingPath": row["heading_path"],
                "matchKind": match_kind,
                "matchedTerms": matched_terms,
                "retrieval": {
                    "coreObjects": core_objects,
                    "coreHits": core_hits,
                    "coreMatchedTerms": actual_core_terms,
                    "scopeTerms": scope_terms,
                    "scopeHits": scope_matches[row["chunk_id"]],
                    "requestedAttribute": roles.get("requestedAttribute", ""),
                    "coreConfidence": roles.get("coreConfidence", "low"),
                    "reason": reason,
                    "matchedTerms": matched_terms,
                    "indexTerms": [],
                    "matchLocations": locations,
                    "previewMatchedTerms": [term for term in body_terms if contains_term(snippet, term)],
                    "outsidePreviewTerms": [term for term in body_terms if not contains_term(snippet, term)],
                    "descriptionMatchedTerms": description_matches.get(linked.get("objectId"), []) if linked else [],
                    "objectNames": object_names,
                    "chain": chain,
                },
                "indexHit": True,
            }
        )
        if len(results) >= limit:
            break
    stats = index_stats(connection)
    connection.close()
    return {
        "results": results,
        "index": {
            "ok": True,
            "terms": terms,
            "build": build_info,
            "candidateCount": len(candidate_ids),
            "queryRoles": roles,
            "coreVerified": verified_core,
            "elapsedMs": round((time.perf_counter() - started) * 1000, 2),
            "retrievalMode": retrieval_mode,
            "globalFtsUsed": global_fts_used,
            "objectLinkedChunks": len(linked_scores),
            "linkedSourceDocuments": len(linked_source_document_ids),
            "fallbackReason": fallback_reason,
            **stats,
        },
    }


def object_residual_terms(terms, object_matches):
    aliases = sorted(
        {row["normalized_alias"] for row in object_matches if row["normalized_alias"]},
        key=len,
        reverse=True,
    )
    residuals = []
    seen = set()
    for term in terms:
        residual = canonical_key(term)
        for alias in aliases:
            residual = residual.replace(alias, "")
        residual = re.sub(
            r"请问|关于|查询|检索|解释|说明|是什么|是啥|什么意思|有哪些|如何|怎么|为何|为什么|作用|要求|规则|流程|条件",
            "",
            residual,
        )
        if len(residual) >= 2 and residual not in seen:
            seen.add(residual)
            residuals.append(residual)
    return residuals


def description_object_matches(connection, query, limit=8, phrases=None):
    """Match LLM descriptions lexically; evidence still comes only from object_mentions."""
    from .query_roles import meaningful_phrases, contains_term
    phrases = phrases if phrases is not None else meaningful_phrases(query)
    matches = []
    for row in connection.execute(
        """
        SELECT k.id AS object_id, k.canonical_name, k.normalized_name, k.domain_type,
               k.description,
               coalesce(group_concat(CASE WHEN t.term_kind='keyword' THEN t.term END, ' '), '') AS keywords
        FROM knowledge_objects k
        LEFT JOIN object_terms t ON t.object_id=k.id
        WHERE k.description<>''
          AND EXISTS (
              SELECT 1 FROM object_mentions m
              WHERE m.object_id=k.id AND m.raw_chunk_id IS NOT NULL
          )
        GROUP BY k.id
        """
    ):
        complete = [term for term in phrases if len(term) >= 4 and contains_term(row['description'], term)]
        if not complete:
            continue
        matches.append((sum(len(term) for term in complete), {
            "object_id": row["object_id"],
            "canonical_name": row["canonical_name"],
            "normalized_name": row["normalized_name"],
            "domain_type": row["domain_type"],
            "alias": row["canonical_name"],
            "normalized_alias": "",
            "descriptionTerms": complete,
        }))
    matches.sort(key=lambda item: item[0], reverse=True)
    return [row for _, row in matches[:limit]]


def match_kind_for(chunk_id, matched_terms, row, linked_kinds=None):
    linked = (linked_kinds or {}).get(chunk_id)
    if linked:
        if linked["kind"] == "structured":
            return "object-source-structured"
        return "object-name-match"
    for term in matched_terms:
        if row["title"].casefold() == term.casefold():
            return "exact-title"
    if any(len(term) >= 4 and term.casefold() in row["content"].casefold() for term in matched_terms):
        return "exact-phrase"
    return "fulltext"


def relevant_snippet(text, terms, before=220, after=420):
    folded = text.casefold()
    ordered = sorted((term for term in terms if term), key=len, reverse=True)
    for term in ordered:
        index = folded.find(term.casefold())
        if index >= 0:
            return text[max(0, index - before): min(len(text), index + len(term) + after)].strip()
    return text[: before + after].strip()


def get_indexed_document(project, virtual_path):
    connection = connect_index(project)
    if connection is None:
        return None
    row = connection.execute(
        "SELECT id, extracted_text, absolute_path, document_type, title FROM documents WHERE virtual_path = ?",
        (str(virtual_path or "").replace("\\", "/"),),
    ).fetchone()
    result = {**dict(row), **edition_metadata(connection, row["id"])} if row else None
    connection.close()
    return result


def get_indexed_structural_node(project, structural_node_id):
    connection = connect_index(project)
    if connection is None:
        return None
    row = connection.execute(
        """
        SELECT n.id, n.heading_path, c.id AS chunk_id,
               d.absolute_path, d.document_type, d.virtual_path
        FROM structural_nodes n
        JOIN chunks c ON c.structural_node_id=n.id
        JOIN documents d ON d.id=c.document_id
        WHERE n.id=? AND n.node_type='section'
        ORDER BY c.chunk_no LIMIT 1
        """,
        (str(structural_node_id or ""),),
    ).fetchone()
    connection.close()
    if not row:
        return None
    evidence = get_indexed_section_evidence(project, [row["chunk_id"]], max_sections=1)
    return {
        "content": evidence[0]["content"] if evidence else "",
        "headingPath": row["heading_path"],
        "absolutePath": row["absolute_path"],
        "path": row["virtual_path"],
        "documentType": row["document_type"],
    }


def get_indexed_chunks(project, chunk_ids):
    ids = [int(value) for value in chunk_ids if str(value).isdigit()]
    if not ids:
        return []
    connection = connect_index(project)
    if connection is None:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT c.id, c.document_version_id, c.structural_node_id, c.content, c.heading_path,
               c.page_no, c.start_offset, c.end_offset, d.id AS document_id,
               d.virtual_path, d.relative_path, d.title
        FROM chunks c JOIN documents d ON d.id = c.document_id
        WHERE c.id IN ({placeholders})
        """,
        tuple(ids),
    ).fetchall()
    connection.close()
    by_id = {row["id"]: dict(row) for row in rows}
    return [by_id[value] for value in ids if value in by_id]


def inspect_project_index(project, document_id=None, chunk_id=None):
    connection = connect_index(project)
    if connection is None:
        return {"ok": False, "reason": "搜索索引尚未构建"}
    if get_meta(connection, "index_version") != INDEX_VERSION:
        connection.close()
        return {"ok": False, "reason": f"索引版本不是 V{INDEX_VERSION}，请先重建"}

    if chunk_id is not None:
        chunk_id = int(chunk_id)
        row = connection.execute(
            """
            SELECT c.*, d.virtual_path, d.relative_path, d.title, d.document_type
            FROM chunks c JOIN documents d ON d.id=c.document_id WHERE c.id=?
            """,
            (chunk_id,),
        ).fetchone()
        if not row:
            connection.close()
            return {"ok": False, "reason": "Chunk 不存在"}
        objects = [dict(value) for value in connection.execute(
            """
            SELECT m.mention_id, o.id, o.canonical_name AS name, o.domain_type,
                   o.ontology_kind, o.description, o.status,
                   m.matched_text, m.evidence, m.extraction_summary, m.confidence,
                   COALESCE((SELECT group_concat(t.term, '、') FROM object_terms t
                             WHERE t.object_id=o.id AND t.term_kind='alias'), '') AS aliases,
                   COALESCE((SELECT group_concat(t.term, '、') FROM object_terms t
                             WHERE t.object_id=o.id AND t.term_kind='keyword'), '') AS keywords
            FROM object_mentions m JOIN knowledge_objects o ON o.id=m.object_id
            WHERE m.raw_chunk_id=? ORDER BY o.canonical_name, m.mention_id
            """,
            (chunk_id,),
        )]
        claims = [dict(value) for value in connection.execute(
            """
            SELECT c.claim_id, subject.canonical_name AS subject, c.predicate,
                   COALESCE(target.canonical_name, c.literal_value) AS object,
                   e.quoted_text, e.score
            FROM claim_evidence e
            JOIN claims c ON c.claim_id=e.claim_id
            JOIN knowledge_objects subject ON subject.id=c.subject_object_id
            LEFT JOIN knowledge_objects target ON target.id=c.object_object_id
            WHERE e.chunk_id=? ORDER BY subject.canonical_name, c.predicate
            """,
            (chunk_id,),
        )]
        adjacent = connection.execute(
            """
            SELECT
              (SELECT id FROM chunks WHERE document_id=? AND chunk_no<? ORDER BY chunk_no DESC LIMIT 1) AS previous_id,
              (SELECT id FROM chunks WHERE document_id=? AND chunk_no>? ORDER BY chunk_no LIMIT 1) AS next_id
            """,
            (row["document_id"], row["chunk_no"], row["document_id"], row["chunk_no"]),
        ).fetchone()
        connection.close()
        evidence = get_indexed_section_evidence(project, [chunk_id], max_sections=1)
        return {
            "ok": True,
            "chunk": {**dict(row), "charCount": len(row["content"])},
            "objects": objects,
            "claims": claims,
            "answerEvidence": evidence[0] if evidence else None,
            "previousChunkId": adjacent["previous_id"],
            "nextChunkId": adjacent["next_id"],
        }

    if document_id is not None:
        document_id = int(document_id)
        document = connection.execute(
            """
            SELECT d.id, d.virtual_path, d.relative_path, d.title, d.document_type,
                   d.size, d.parser_version, d.indexed_at, d.status,
                   v.id AS document_version_id, v.extracted_text AS parsed_text
            FROM documents d
            LEFT JOIN document_versions v ON v.id=(
                SELECT document_version_id FROM chunks WHERE document_id=d.id ORDER BY chunk_no LIMIT 1
            )
            WHERE d.id=?
            """,
            (document_id,),
        ).fetchone()
        if not document:
            connection.close()
            return {"ok": False, "reason": "文档不存在"}
        sections = []
        for value in connection.execute(
            """
            SELECT id, parent_id, ordinal, heading, heading_path, start_offset, end_offset
            FROM structural_nodes
            WHERE document_version_id=? AND node_type='section'
            ORDER BY ordinal
            """,
            (document["document_version_id"],),
        ):
            item = dict(value)
            item["depth"] = max(0, item["heading_path"].count(" > ") - 1)
            item["charCount"] = max(0, item["end_offset"] - item["start_offset"])
            sections.append(item)
        chunks = []
        for value in connection.execute(
            """
            SELECT c.id, c.structural_node_id, c.chunk_no, c.heading_path, c.page_no,
                   c.start_offset, c.end_offset, c.content,
                   (SELECT count(*) FROM object_mentions m WHERE m.raw_chunk_id=c.id) AS object_count,
                   (SELECT count(*) FROM claim_evidence e WHERE e.chunk_id=c.id) AS claim_count
            FROM chunks c WHERE c.document_id=? ORDER BY c.chunk_no
            """,
            (document_id,),
        ):
            item = dict(value)
            item["charCount"] = len(item["content"])
            warnings = []
            if not item["heading_path"]:
                warnings.append("无标题路径")
            if item["charCount"] < 120:
                warnings.append("内容较短")
            if item["charCount"] > 1400:
                warnings.append("超过目标长度")
            if not item["object_count"] and not item["claim_count"]:
                warnings.append("未抽取对象/Claim")
            item["warnings"] = warnings
            chunks.append(item)
        connection.close()
        return {
            "ok": True,
            "document": {**dict(document), "parsedCharCount": len(document["parsed_text"] or "")},
            "sections": sections,
            "chunks": chunks,
        }

    documents = [dict(value) for value in connection.execute(
        """
        SELECT d.id, d.virtual_path, d.relative_path, d.title, d.document_type,
               d.size, d.parser_version, d.indexed_at, d.status,
               (SELECT count(*) FROM chunks c WHERE c.document_id=d.id) AS chunk_count,
               (SELECT count(*) FROM structural_nodes n JOIN document_versions v ON v.id=n.document_version_id
                WHERE v.document_id=d.id AND n.node_type='section') AS section_count
        FROM documents d ORDER BY d.relative_path
        """
    )]
    connection.close()
    return {"ok": True, "documents": documents}


def get_indexed_section_evidence(project, chunk_ids, max_sections=8):
    if not project.get("_allVersions"):
        project = scope_project(project, project.get("_versionScope"))
    ids = [int(value) for value in chunk_ids if str(value).isdigit()]
    if not ids:
        return []
    connection = connect_index(project)
    if connection is None:
        return []
    placeholders = ",".join("?" for _ in ids)
    rows = connection.execute(
        f"""
        SELECT c.id AS chunk_id, c.structural_node_id, c.document_version_id, c.content AS chunk_content,
               d.id AS document_id, d.virtual_path, d.relative_path, d.title, v.extracted_text
        FROM chunks c
        JOIN documents d ON d.id=c.document_id
        JOIN document_versions v ON v.id=c.document_version_id
        WHERE c.id IN ({placeholders})
        """,
        tuple(ids),
    ).fetchall()
    by_chunk = {row["chunk_id"]: row for row in rows}
    evidence = []
    by_section = {}
    included_public = set()
    for chunk_id in ids:
        row = by_chunk.get(chunk_id)
        if not row:
            continue
        key = (row["document_version_id"], row["structural_node_id"])
        node_type = connection.execute("SELECT node_type FROM structural_nodes WHERE id=?", (row["structural_node_id"],)).fetchone()
        if node_type and node_type["node_type"] == "document":
            key = (*key, chunk_id)
        if key in by_section:
            by_section[key]["chunkIds"].append(chunk_id)
            continue
        if len(evidence) >= max_sections:
            break
        node = connection.execute(
            "SELECT * FROM structural_nodes WHERE id=?", (row["structural_node_id"],)
        ).fetchone()
        if not node:
            continue
        ancestors = []
        parent_id = node["parent_id"]
        while parent_id:
            parent = connection.execute("SELECT * FROM structural_nodes WHERE id=?", (parent_id,)).fetchone()
            if not parent:
                break
            ancestors.append(parent)
            parent_id = parent["parent_id"]
        text = row["extracted_text"]
        parts = []
        public_sections = []
        for ancestor in reversed(ancestors):
            public_key = (row["document_version_id"], ancestor["id"])
            if public_key in included_public:
                continue
            direct_blocks = connection.execute(
                """
                SELECT start_offset, end_offset FROM structural_nodes
                WHERE parent_id=? AND node_type IN ('paragraph', 'list', 'table')
                ORDER BY ordinal
                """,
                (ancestor["id"],),
            ).fetchall()
            public_text = "\n\n".join(
                text[block["start_offset"]:block["end_offset"]].strip()
                for block in direct_blocks
                if text[block["start_offset"]:block["end_offset"]].strip()
            )
            if not public_text:
                continue
            label = ancestor["heading_path"] or row["title"]
            heading = f"{ancestor['heading']}\n\n" if ancestor["heading"] else ""
            parts.append(f"[上级公共内容：{label}]\n{heading}{public_text}")
            public_sections.append(ancestor["id"])
            included_public.add(public_key)
        section_text = (row["chunk_content"] if node["node_type"] == "document" else text[node["start_offset"]:node["end_offset"]]).strip()
        label = node["heading_path"] or row["title"]
        evidence_label = "命中文档片段" if node["node_type"] == "document" else "命中章节完整原文"
        parts.append(f"[{evidence_label}：{label}]\n{section_text}")
        item = {
            "id": node["id"], "path": row["virtual_path"], "title": row["title"],
            "documentVersionId": row["document_version_id"],
            **edition_metadata(connection, row["document_id"]),
            "heading_path": node["heading_path"], "content": "\n\n".join(parts),
            "section_chars": len(section_text), "complete": True,
            "publicSectionIds": public_sections, "chunkIds": [chunk_id],
        }
        evidence.append(item)
        by_section[key] = item
    connection.close()
    return evidence


def get_project_graph(project):
    connection = connect_index(scope_project(project, project.get("_versionScope")))
    if connection is None:
        return None
    knowledge_table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'knowledge_objects'"
    ).fetchone()
    if get_meta(connection, "index_version") != INDEX_VERSION or not knowledge_table:
        connection.close()
        return None

    nodes = {}
    object_paths = {}
    object_claims = {}
    for row in connection.execute(
        """
        SELECT o.id, o.object_key, o.canonical_name, o.domain_type, o.ontology_kind,
               o.description, o.status
        FROM knowledge_objects o
        ORDER BY o.canonical_name
        """
    ):
        node_id = f"object:{row['object_key']}"
        object_paths[row["id"]] = node_id
        nodes[node_id] = {
            "id": node_id,
            "label": row["canonical_name"],
            "type": row["domain_type"],
            "ontologyKind": row["ontology_kind"],
            "status": row["status"],
            "description": row["description"],
            "aliases": [],
            "keywords": [],
            "claims": [],
            "path": "",
        }
        object_claims[row["id"]] = []

    for row in connection.execute(
        "SELECT object_id, term, term_kind FROM object_terms WHERE term_kind<>'canonical' ORDER BY term_kind, term"
    ):
        node_id = object_paths.get(row["object_id"])
        if node_id:
            nodes[node_id]["keywords" if row["term_kind"] == "keyword" else "aliases"].append(row["term"])

    links = []
    for row in connection.execute(
        """
        SELECT c.claim_id, c.subject_object_id, c.predicate, c.object_object_id,
               c.literal_value, c.qualifiers_json, c.status,
               (SELECT count(*) FROM claim_evidence e WHERE e.claim_id = c.claim_id) AS evidence_count
        FROM claims c ORDER BY c.claim_id
        """
    ):
        source = object_paths.get(row["subject_object_id"])
        target = object_paths.get(row["object_object_id"])
        if not target and row["literal_value"]:
            target = f"claim-value:{row['claim_id']}"
            nodes[target] = {
                "id": target,
                "label": row["literal_value"],
                "type": "value",
                "path": "",
            }
        if not source or not target:
            continue
        try:
            qualifiers = json.loads(row["qualifiers_json"] or "{}")
        except json.JSONDecodeError:
            qualifiers = {}
        links.append({
            "source": source,
            "target": target,
            "type": row["predicate"],
            "claimId": row["claim_id"],
            "qualifiers": qualifiers,
            "status": row["status"],
            "evidenceCount": row["evidence_count"],
            "directed": True,
        })
        claim_text = f"{nodes[source]['label']} {row['predicate']} {nodes[target]['label']}"
        if qualifiers:
            claim_text += "（" + "；".join(f"{key}：{value}" for key, value in qualifiers.items()) + "）"
        object_claims[row["subject_object_id"]].append(claim_text)
        if row["object_object_id"] in object_claims:
            object_claims[row["object_object_id"]].append(claim_text)

    for row in connection.execute(
        """
        SELECT n.id, n.parent_id, n.heading, n.heading_path,
               parent.node_type AS parent_type,
               d.id AS source_document_id, d.virtual_path, d.relative_path, d.title
        FROM structural_nodes n
        JOIN document_versions v ON v.id=n.document_version_id
        JOIN documents d ON d.id=v.document_id
        LEFT JOIN structural_nodes parent ON parent.id=n.parent_id
        WHERE n.node_type='section' AND d.document_type='raw'
        ORDER BY d.relative_path, n.ordinal
        """
    ):
        section_id = f"section:{row['id']}"
        document_id = f"source:{row['virtual_path']}"
        parent_id = f"section:{row['parent_id']}" if row["parent_type"] == "section" else document_id
        nodes[section_id] = {
            "id": section_id, "label": row["heading_path"], "type": "section",
            "chapterTitle": row["heading"], "path": row["virtual_path"], "structuralNodeId": row["id"],
            **edition_metadata(connection, row["source_document_id"]),
        }
        nodes.setdefault(document_id, {
            "id": document_id, "label": document_structure_path(row["relative_path"]),
            "type": "source", "path": row["virtual_path"],
            **edition_metadata(connection, row["source_document_id"]),
        })
        links.append({"source": section_id, "target": parent_id, "type": "belongs-to", "directed": True})

    for row in connection.execute(
        """
        SELECT DISTINCT m.object_id, m.structural_node_id, d.virtual_path, d.title,
               n.node_type, n.heading_path
        FROM object_mentions m
        JOIN chunks c ON c.id=m.raw_chunk_id
        JOIN documents d ON d.id=c.document_id
        JOIN structural_nodes n ON n.id=m.structural_node_id
        WHERE m.raw_chunk_id IS NOT NULL AND m.structural_node_id IS NOT NULL
        ORDER BY m.object_id, n.heading_path
        """
    ):
        source = object_paths.get(row["object_id"])
        if not source:
            continue
        section_id = f"section:{row['structural_node_id']}"
        document_id = f"source:{row['virtual_path']}"
        nodes.setdefault(document_id, {
            "id": document_id, "label": row["title"], "type": "source", "path": row["virtual_path"],
        })
        target = document_id
        if row["node_type"] == "section":
            nodes.setdefault(section_id, {
                "id": section_id, "label": row["heading_path"], "type": "section",
                "path": row["virtual_path"], "structuralNodeId": row["structural_node_id"],
            })
            target = section_id
        links.append({"source": source, "target": target, "type": "mention", "directed": True})

    # Kept for the original relation/source graph; the structured view filters these direct source edges out.
    for row in connection.execute(
        """
        SELECT s.object_id, d.id AS source_document_id, d.virtual_path, d.title
        FROM object_sources s JOIN documents d ON d.id=s.document_id
        ORDER BY s.object_id, d.virtual_path
        """
    ):
        source = object_paths.get(row["object_id"])
        if not source:
            continue
        target = f"source:{row['virtual_path']}"
        nodes.setdefault(target, {
            "id": target, "label": row["title"], "type": "source", "path": row["virtual_path"],
        })
        nodes[target].update(edition_metadata(connection, row["source_document_id"]))
        links.append({"source": source, "target": target, "type": "source", "directed": True})

    for object_id, node_id in object_paths.items():
        node = nodes[node_id]
        node["claims"] = object_claims[object_id]
        parts = ["## 聚合摘要", node["description"] or "暂无聚合摘要。"]
        if node["aliases"]:
            parts.extend(("", "## 别名", *[f"- {value}" for value in node["aliases"]]))
        if node["keywords"]:
            parts.extend(("", "## 关键词", *[f"- {value}" for value in node["keywords"]]))
        if node["claims"]:
            parts.extend(("", "## Claim", *[f"- {value}" for value in node["claims"]]))
        node["content"] = "\n".join(parts)

    connection.close()
    return {"nodes": list(nodes.values()), "links": links, "graphVersion": 5}


def index_stats(connection_or_project):
    close = False
    if isinstance(connection_or_project, sqlite3.Connection):
        connection = connection_or_project
    else:
        connection = connect_index(connection_or_project)
        close = True
    if connection is None:
        return {
            "documents": 0, "documentVersions": 0, "structuralNodes": 0, "chunks": 0,
            "knowledgeObjects": 0, "objectTerms": 0, "objectMentions": 0,
            "claims": 0, "claimEvidence": 0,
            "lastBuildAt": None,
        }
    result = {
        "documents": table_count(connection, "documents"),
        "documentVersions": table_count(connection, "document_versions"),
        "structuralNodes": table_count(connection, "structural_nodes"),
        "chunks": table_count(connection, "chunks"),
        "knowledgeObjects": table_count(connection, "knowledge_objects"),
        "objectTerms": table_count(connection, "object_terms"),
        "objectMentions": table_count(connection, "object_mentions"),
        "claims": table_count(connection, "claims"),
        "claimEvidence": table_count(connection, "claim_evidence"),
        "lastBuildAt": get_meta(connection, "last_build_at"),
        "indexVersion": get_meta(connection, "index_version"),
        "parserVersion": get_meta(connection, "parser_version"),
        "tokenizerVersion": get_meta(connection, "tokenizer_version"),
        "buildComplete": get_meta(connection, "build_complete", False),
        "lastBuildFailures": get_meta(connection, "last_build_failures", []),
        "lastAttemptAt": get_meta(connection, "last_attempt_at"),
        "lastAttemptFailures": get_meta(connection, "last_attempt_failures", []),
    }
    if close:
        connection.close()
    return result


def table_count(connection, table_name):
    exists = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table_name,)
    ).fetchone()
    if not exists:
        return 0
    return connection.execute(f'SELECT count(*) FROM "{table_name}"').fetchone()[0]


def verify_project_index(project):
    connection = connect_index(project)
    if connection is None:
        return {"ok": False, "reason": "索引不存在", **index_stats(project)}
    integrity = connection.execute("PRAGMA integrity_check").fetchone()[0]
    stats = index_stats(connection)
    version_ok = stats.get("indexVersion") == INDEX_VERSION
    if not version_ok:
        connection.close()
        return {
            "ok": False,
            "reason": f"索引版本为 {stats.get('indexVersion')}，需要升级到 {INDEX_VERSION}",
            "integrity": integrity,
            "versionOk": False,
            **stats,
        }
    orphan_chunks = connection.execute(
        """
        SELECT count(*) FROM chunks c
        LEFT JOIN documents d ON d.id = c.document_id
        LEFT JOIN document_versions v ON v.id = c.document_version_id
        LEFT JOIN structural_nodes n ON n.id = c.structural_node_id
        WHERE d.id IS NULL OR v.id IS NULL OR c.structural_node_id IS NULL OR n.id IS NULL
        """
    ).fetchone()[0]
    missing_fts = connection.execute(
        "SELECT count(*) FROM chunks c LEFT JOIN chunk_fts f ON f.rowid = c.id WHERE f.rowid IS NULL"
    ).fetchone()[0]
    orphan_knowledge_objects = connection.execute(
        """
        SELECT count(*) FROM object_mentions m
        LEFT JOIN knowledge_objects o ON o.id=m.object_id
        LEFT JOIN document_versions v ON v.id=m.document_version_id
        LEFT JOIN structural_nodes n ON n.id=m.structural_node_id
        LEFT JOIN chunks c ON c.id=m.raw_chunk_id
        WHERE o.id IS NULL OR v.id IS NULL OR m.structural_node_id IS NULL OR n.id IS NULL
          OR m.raw_chunk_id IS NULL OR c.id IS NULL
        """
    ).fetchone()[0] if table_count(connection, "object_mentions") else 0
    orphan_claims = connection.execute(
        """
        SELECT count(*) FROM claims c
        LEFT JOIN knowledge_objects s ON s.id = c.subject_object_id
        LEFT JOIN knowledge_objects o ON o.id = c.object_object_id
        WHERE s.id IS NULL OR (c.object_object_id IS NOT NULL AND o.id IS NULL)
        """
    ).fetchone()[0] if table_count(connection, "claims") else 0
    orphan_claim_evidence = connection.execute(
        """
        SELECT count(*) FROM claim_evidence e
        LEFT JOIN claims c ON c.claim_id = e.claim_id
        LEFT JOIN documents d ON d.id = e.document_id
        LEFT JOIN document_versions v ON v.id = e.document_version_id
        LEFT JOIN structural_nodes n ON n.id = e.structural_node_id
        LEFT JOIN chunks k ON k.id = e.chunk_id
        WHERE c.claim_id IS NULL OR d.id IS NULL OR v.id IS NULL
          OR e.structural_node_id IS NULL OR n.id IS NULL
          OR (e.chunk_id IS NOT NULL AND k.id IS NULL)
        """
    ).fetchone()[0] if table_count(connection, "claim_evidence") else 0
    build_complete = bool(stats.get("buildComplete"))
    connection.close()
    return {
        "ok": (
            integrity == "ok" and orphan_chunks == 0 and missing_fts == 0
            and orphan_knowledge_objects == 0
            and orphan_claims == 0 and orphan_claim_evidence == 0
            and version_ok and build_complete
        ),
        "integrity": integrity,
        "orphanChunks": orphan_chunks,
        "missingFtsRows": missing_fts,
        "orphanKnowledgeObjects": orphan_knowledge_objects,
        "orphanClaims": orphan_claims,
        "orphanClaimEvidence": orphan_claim_evidence,
        "versionOk": version_ok,
        **stats,
    }
