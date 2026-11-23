"""Durable document snapshots and the shared retrieval version boundary.

Each published edition has its own documents row. Existing chunk/evidence foreign
keys therefore remain usable without changing the V7 extraction payload format.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import uuid
from datetime import date
from pathlib import Path

from .file_paths import fs_path


def history_dir(project):
    return fs_path(Path(project["path"]).resolve() / "document-history")


def create_version_schema(connection):
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS document_editions (
            document_id INTEGER PRIMARY KEY REFERENCES documents(id) ON DELETE CASCADE,
            uid TEXT NOT NULL UNIQUE,
            logical_id TEXT NOT NULL,
            name TEXT NOT NULL,
            label TEXT NOT NULL,
            source_ref TEXT NOT NULL,
            file_hash TEXT NOT NULL,
            snapshot TEXT NOT NULL,
            published_at TEXT NOT NULL DEFAULT '',
            effective_at TEXT NOT NULL DEFAULT '',
            is_default INTEGER NOT NULL DEFAULT 0 CHECK(is_default IN (0,1)),
            UNIQUE(logical_id, label)
        );
        CREATE UNIQUE INDEX IF NOT EXISTS edition_one_default
            ON document_editions(logical_id) WHERE is_default=1;
        CREATE TABLE IF NOT EXISTS object_version_profiles (
            object_id INTEGER NOT NULL REFERENCES knowledge_objects(id) ON DELETE CASCADE,
            document_id INTEGER NOT NULL REFERENCES documents(id) ON DELETE CASCADE,
            description TEXT NOT NULL DEFAULT '',
            terms_json TEXT NOT NULL DEFAULT '[]',
            PRIMARY KEY(object_id, document_id)
        );
    """)


def read_catalog(project):
    # Recover a committed index even if the process stopped before manifest export.
    from .search_index import index_path
    database = index_path(project)
    if database.exists():
        connection = sqlite3.connect(f"file:{database.as_posix()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        try:
            if connection.execute("SELECT 1 FROM sqlite_master WHERE name='document_editions'").fetchone():
                rows = connection.execute("SELECT e.*, d.virtual_path, d.relative_path FROM document_editions e JOIN documents d ON d.id=e.document_id ORDER BY e.document_id").fetchall()
                if rows:
                    return [dict(row) for row in rows]
        finally:
            connection.close()
    path = history_dir(project) / "catalog.json"
    if not path.exists():
        return []
    return json.loads(path.read_text(encoding="utf-8"))["editions"]


def save_catalog(project, connection):
    # Caller holds the project build lock; a staged import saves only after publish.
    rows = connection.execute("""
        SELECT e.*, d.virtual_path, d.relative_path FROM document_editions e
        JOIN documents d ON d.id=e.document_id ORDER BY e.document_id
    """).fetchall()
    directory = history_dir(project)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / "catalog.json.tmp"
    temporary.write_text(json.dumps({"editions": [
        {key: row[key] for key in row.keys() if key != "document_id"} for row in rows
    ]}, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, directory / "catalog.json")


def discover_editions(project, live_items):
    catalog = read_catalog(project)
    items = {}
    for entry in catalog:
        snapshot = (history_dir(project) / entry["snapshot"]).resolve()
        if not snapshot.is_relative_to(history_dir(project)) or not snapshot.is_file():
            raise ValueError(f"历史原文快照缺失：{entry['source_ref']} · {entry['label']}")
        if hashlib.sha256(snapshot.read_bytes()).hexdigest() != entry["file_hash"]:
            raise ValueError(f"历史原文快照校验失败：{entry['source_ref']} · {entry['label']}")
        items[entry["uid"]] = {"path": snapshot, "virtual_path": entry["virtual_path"],
                               "relative_path": entry["relative_path"], "document_type": "raw",
                               "edition": entry}
    for item in live_items:
        content = item["path"].read_bytes()
        digest = hashlib.sha256(content).hexdigest()
        same_source = [entry for entry in catalog if entry["source_ref"] == item["relative_path"]]
        existing = next((entry for entry in same_source if entry["file_hash"] == digest), None)
        if existing:
            items[existing["uid"]]["live_source"] = item["relative_path"]
            continue
        uid = uuid.uuid4().hex
        first = same_source[0] if same_source else None
        relative = f"__versions/{uid}/{Path(item['relative_path']).name}" if first else item["relative_path"]
        label = f"修订-{uid[:8]}" if first else "初始版本"
        snapshot_ref = f"{digest}{item['path'].suffix.lower()}"
        snapshot = history_dir(project) / snapshot_ref
        snapshot.parent.mkdir(parents=True, exist_ok=True)
        if not snapshot.exists():
            temporary = snapshot.with_name(f"{snapshot.name}.{uid}.tmp")
            temporary.write_bytes(content)
            shutil.copystat(item["path"], temporary)
            os.replace(temporary, snapshot)
        entry = {"uid": uid, "logical_id": first["logical_id"] if first else uid,
                 "name": first["name"] if first else item["path"].stem, "label": label,
                 "source_ref": item["relative_path"], "file_hash": digest,
                 "snapshot": snapshot_ref, "published_at": "", "effective_at": "",
                 "is_default": 0 if first else 1,
                 "virtual_path": f"raw/sources/{relative}", "relative_path": relative}
        items[uid] = {**item, "path": snapshot, "virtual_path": entry["virtual_path"],
                      "relative_path": relative, "edition": entry, "live_source": item["relative_path"]}
    return list(items.values())


def register_edition(connection, item, document_id):
    entry = item.get("edition")
    if not entry:
        return
    connection.execute("UPDATE documents SET absolute_path=? WHERE id=?", (str(item["path"]), document_id))
    columns = ("uid", "logical_id", "name", "label", "source_ref", "file_hash", "snapshot",
               "published_at", "effective_at", "is_default")
    connection.execute(f"INSERT OR IGNORE INTO document_editions(document_id,{','.join(columns)}) "
                       f"VALUES ({','.join('?' for _ in range(len(columns)+1))})",
                       (document_id, *(entry[key] for key in columns)))


def backup_before_migration(project):
    from .search_index import index_path
    path = index_path(project)
    if not path.exists():
        return
    source = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    try:
        if source.execute("SELECT 1 FROM sqlite_master WHERE name='document_editions'").fetchone():
            return
        target = path.with_name(path.name + ".pre-versions.bak")
        if not target.exists():
            destination = sqlite3.connect(target)
            try:
                source.backup(destination)
            finally:
                destination.close()
    finally:
        source.close()


def migrate_legacy_editions(project, connection):
    """Preserve indexed content when the original was overwritten before upgrade."""
    from .search_index import read_indexable_document
    rows = connection.execute("SELECT * FROM documents WHERE id NOT IN (SELECT document_id FROM document_editions)").fetchall()
    with connection:
        for row in rows:
            source = Path(row["absolute_path"])
            matches = False
            if source.is_file():
                try:
                    parsed = read_indexable_document(source)["text"]
                    matches = hashlib.sha256(parsed.encode("utf-8", errors="ignore")).hexdigest() == row["content_hash"]
                except Exception:
                    matches = False
            content = source.read_bytes() if matches else row["extracted_text"].encode("utf-8")
            digest = hashlib.sha256(content).hexdigest()
            suffix = source.suffix.lower() if matches else ".txt"
            snapshot_ref = f"{digest}{suffix}"
            snapshot = history_dir(project) / snapshot_ref
            snapshot.parent.mkdir(parents=True, exist_ok=True)
            if not snapshot.exists():
                snapshot.write_bytes(content)
            uid = uuid.uuid4().hex
            entry = {"uid": uid, "logical_id": uid, "name": row["title"],
                     "label": "迁移版本" if matches else "迁移版本（仅文本）",
                     "source_ref": row["relative_path"], "file_hash": digest, "snapshot": snapshot_ref,
                     "published_at": "", "effective_at": "", "is_default": 1}
            register_edition(connection, {"path": snapshot, "edition": entry}, row["id"])
            stat = snapshot.stat()
            connection.execute("UPDATE documents SET size=?, mtime_ns=? WHERE id=?", (stat.st_size, stat.st_mtime_ns, row["id"]))


def edition_metadata(connection, document_id):
    if not connection.execute("SELECT 1 FROM main.sqlite_master WHERE name='document_editions'").fetchone():
        return {}
    row = connection.execute("SELECT * FROM main.document_editions WHERE document_id=?", (document_id,)).fetchone()
    return {"editionId": row["uid"], "logicalDocumentId": row["logical_id"],
            "documentName": row["name"], "versionLabel": row["label"],
            "isDefaultVersion": bool(row["is_default"])} if row else {}


def list_editions(project):
    from .search_index import connect_index
    connection = connect_index(project)
    if connection is None:
        return {"documents": []}
    try:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE name='document_editions'").fetchone():
            return {"documents": [], "needsMigration": True}
        groups = {}
        for row in connection.execute("""
            SELECT e.*, d.virtual_path, d.indexed_at,
                   (SELECT count(*) FROM chunks c WHERE c.document_id=d.id) AS chunk_count
            FROM document_editions e JOIN documents d ON d.id=e.document_id ORDER BY d.id
        """):
            group = groups.setdefault(row["logical_id"], {"id": row["logical_id"], "name": row["name"], "versions": []})
            group["versions"].append({**edition_metadata(connection, row["document_id"]),
                "documentId": row["document_id"], "path": row["virtual_path"],
                "sourceRef": row["source_ref"], "publishedAt": row["published_at"],
                "effectiveAt": row["effective_at"], "chunkCount": row["chunk_count"],
                "status": "ready" if row["chunk_count"] else "empty"})
        missing_profiles = connection.execute("""SELECT count(*) FROM object_sources s
            WHERE NOT EXISTS(SELECT 1 FROM object_version_profiles p
                             WHERE p.object_id=s.object_id AND p.document_id=s.document_id)""").fetchone()[0]
        unavailable = connection.execute("SELECT count(*) FROM document_versions v WHERE length(v.extracted_text)>0 AND NOT EXISTS(SELECT 1 FROM chunks c WHERE c.document_version_id=v.id)").fetchone()[0]
        return {"documents": list(groups.values()), "needsReingestion": bool(missing_profiles),
                "unavailableHistoryCount": unavailable}
    finally:
        connection.close()


def update_edition(project, payload):
    from .search_index import _build_lock, connect_index, set_meta
    import time
    uid = payload.get("editionId")
    if not isinstance(uid, str):
        raise ValueError("缺少 editionId")
    with _build_lock(project):
        connection = connect_index(project, create=True)
        try:
            with connection:
                row = connection.execute("SELECT * FROM document_editions WHERE uid=?", (uid,)).fetchone()
                if not row:
                    raise ValueError("文档版本不存在")
                logical_id = payload.get("logicalDocumentId", row["logical_id"])
                if not isinstance(logical_id, str):
                    raise ValueError("所属文档ID须为字符串")
                target = connection.execute("SELECT * FROM document_editions WHERE logical_id=? LIMIT 1", (logical_id,)).fetchone()
                if not target:
                    raise ValueError("所属文档不存在")
                label = payload.get("versionLabel", row["label"])
                name = payload.get("documentName", target["name"])
                if not isinstance(label, str) or not label.strip() or len(label) > 100:
                    raise ValueError("版本标签须为 1–100 字符")
                if not isinstance(name, str) or not name.strip() or len(name) > 200:
                    raise ValueError("文档名称须为 1–200 字符")
                dates = {}
                for key, column in (("publishedAt", "published_at"), ("effectiveAt", "effective_at")):
                    value = payload.get(key, row[column])
                    if not isinstance(value, str):
                        raise ValueError("日期须为 YYYY-MM-DD 或空字符串")
                    if value:
                        if len(value) != 10 or value[4] != '-' or value[7] != '-':
                            raise ValueError("日期须为 YYYY-MM-DD")
                        date.fromisoformat(value)
                    dates[column] = value
                default = payload.get("setDefault", False)
                if type(default) is not bool:
                    raise ValueError("setDefault 必须为布尔值")
                if default and not connection.execute("SELECT 1 FROM chunks WHERE document_id=?", (row["document_id"],)).fetchone():
                    raise ValueError("空版本不能设为默认")
                if connection.execute("SELECT 1 FROM document_editions WHERE logical_id=? AND label=? AND uid<>?",
                                      (logical_id, label.strip(), uid)).fetchone():
                    raise ValueError("同一文档的版本标签不能重复")
                moving = logical_id != row["logical_id"]
                connection.execute("UPDATE document_editions SET is_default=0 WHERE uid=?", (uid,))
                if default:
                    connection.execute("UPDATE document_editions SET is_default=0 WHERE logical_id=?", (logical_id,))
                connection.execute("""UPDATE document_editions SET logical_id=?, name=?, label=?,
                    published_at=?, effective_at=?, is_default=? WHERE uid=?""",
                    (logical_id, name.strip(), label.strip(), dates["published_at"], dates["effective_at"],
                     int(default or (not moving and row["is_default"])), uid))
                connection.execute("UPDATE document_editions SET name=? WHERE logical_id=?", (name.strip(), logical_id))
                # Moving the default away must leave the old document with a default.
                for group in {logical_id, row["logical_id"]}:
                    if not connection.execute("SELECT 1 FROM document_editions WHERE logical_id=? AND is_default=1", (group,)).fetchone():
                        connection.execute("UPDATE document_editions SET is_default=1 WHERE document_id=(SELECT min(document_id) FROM document_editions WHERE logical_id=?)", (group,))
                set_meta(connection, "last_build_at", time.time())
            save_catalog(project, connection)
        finally:
            connection.close()
    return list_editions(project)


def scope_project(project, scope=None):
    if scope is None:
        scope = {}
    if not isinstance(scope, dict) or any(not isinstance(k, str) or not isinstance(v, str) for k, v in scope.items()):
        raise ValueError("versionScope 必须为 文档ID:版本ID 的对象")
    return {**project, "_versionScope": scope}


def apply_version_scope(connection, scope):
    """TEMP views apply the same boundary to every existing SQL recall branch."""
    if not connection.execute("SELECT 1 FROM main.sqlite_master WHERE name='document_editions'").fetchone():
        if scope:
            raise ValueError("请先迁移文档版本索引")
        return
    rows = connection.execute("SELECT * FROM main.document_editions").fetchall()
    by_group = {}
    for row in rows:
        by_group.setdefault(row["logical_id"], []).append(row)
    if set(scope) - by_group.keys():
        raise ValueError("所选文档不存在，请刷新版本列表")
    selected = []
    for group, editions in by_group.items():
        chosen = next((row for row in editions if row["uid"] == scope[group]), None) if group in scope else next((row for row in editions if row["is_default"]), None)
        if chosen is None:
            raise ValueError("所选版本不存在或不属于该文档，请刷新版本列表")
        selected.append(chosen["document_id"])
    connection.execute("CREATE TEMP TABLE allowed_documents(id INTEGER PRIMARY KEY)")
    connection.executemany("INSERT INTO allowed_documents VALUES(?)", [(value,) for value in selected])
    # A legacy document without edition metadata remains visible until migration.
    connection.execute("INSERT OR IGNORE INTO allowed_documents SELECT id FROM main.documents WHERE id NOT IN (SELECT document_id FROM main.document_editions)")
    for table, predicate in {
        "documents": "id IN (SELECT id FROM allowed_documents)",
        "chunks": "document_id IN (SELECT id FROM allowed_documents)",
        "document_versions": "document_id IN (SELECT id FROM allowed_documents)",
        "structural_nodes": "document_version_id IN (SELECT id FROM document_versions)",
        "object_sources": "document_id IN (SELECT id FROM allowed_documents)",
        "object_mentions": "document_version_id IN (SELECT id FROM document_versions)",
        "claim_evidence": "document_id IN (SELECT id FROM allowed_documents)",
        "claims": "claim_id IN (SELECT claim_id FROM claim_evidence)",
    }.items():
        connection.execute(f"CREATE TEMP VIEW {table} AS SELECT * FROM main.{table} WHERE {predicate}")
    connection.execute("""CREATE TEMP VIEW knowledge_objects AS
        SELECT k.id, k.object_key, k.canonical_name, k.normalized_name, k.domain_type,
               k.ontology_kind, k.status,
               coalesce((SELECT group_concat(p.description, char(10)) FROM main.object_version_profiles p
                         WHERE p.object_id=k.id AND p.document_id IN (SELECT id FROM allowed_documents)), '') AS description
        FROM main.knowledge_objects k WHERE k.id IN (SELECT object_id FROM object_sources)
    """)
    connection.execute("""CREATE TEMP VIEW object_terms AS
        SELECT DISTINCT t.* FROM main.object_terms t JOIN object_sources s ON s.object_id=t.object_id
        JOIN documents d ON d.id=s.document_id
        WHERE EXISTS (SELECT 1 FROM main.object_version_profiles p, json_each(p.terms_json) j
                      WHERE p.object_id=t.object_id AND p.document_id=s.document_id AND lower(j.value)=lower(t.term))
           OR (NOT EXISTS (SELECT 1 FROM main.object_version_profiles p WHERE p.object_id=t.object_id)
               AND instr(lower(d.extracted_text), lower(t.term))>0)
    """)


def record_profiles(connection, object_id, item):
    """Store only source-specific extraction; ambiguous legacy summaries fail closed."""
    profiles = item.get("versionProfiles")
    if profiles is None:
        profiles = []
        for mention in item.get("mentions") or []:
            profiles.append({"chunkId": mention.get("chunkId"), "description": mention.get("summary", ""),
                             "terms": [item.get("name", "")]})
        sources = item.get("sources") or []
        if len(sources) == 1:
            profiles.append({"source": sources[0], "description": item.get("description", ""),
                             "terms": [item.get("name", ""), *(item.get("aliases") or []), *(item.get("keywords") or [])]})
    for profile in profiles:
        if profile.get("chunkId"):
            doc = connection.execute("SELECT document_id FROM chunks WHERE id=?", (profile["chunkId"],)).fetchone()
        else:
            doc = connection.execute("SELECT id AS document_id FROM documents WHERE relative_path=?", (profile.get("source"),)).fetchone()
        if not doc:
            continue
        old = connection.execute("SELECT * FROM object_version_profiles WHERE object_id=? AND document_id=?", (object_id, doc[0])).fetchone()
        descriptions = list(dict.fromkeys(filter(None, [old["description"] if old else "", str(profile.get("description") or "")])))
        terms = list(dict.fromkeys([*(json.loads(old["terms_json"]) if old else []), *(profile.get("terms") or [])]))
        connection.execute("INSERT OR REPLACE INTO object_version_profiles VALUES(?,?,?,?)",
                           (object_id, doc[0], "\n".join(descriptions), json.dumps(terms, ensure_ascii=False)))


def scan_ingestion_sources(project):
    from .ingestion_files import scan_ingestion_sources as scan
    return scan(project)


def validate_source_assignments(project, values, source_files=None, limit_docs=0):
    from .ingestion_files import validate_source_assignments as validate
    return validate(project, values, source_files, limit_docs)
