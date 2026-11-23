import os
import re
from datetime import datetime
from pathlib import Path

from .search_index import INDEX_VERSION, connect_index, get_meta


def export_project_markdown(project):
    connection = connect_index(project)
    if connection is None or get_meta(connection, "index_version") != INDEX_VERSION:
        if connection:
            connection.close()
        raise ValueError("需要先构建 V7 内容索引")

    export_dir = Path(project.get("exportDir") or Path(project["path"]) / "exports" / "wiki").resolve()
    sources_dir = export_dir / "sources"
    objects_dir = export_dir / "objects"
    for folder in (sources_dir, objects_dir, export_dir / "concepts", export_dir / "entities", export_dir / "keywords"):
        clear_markdown(folder)
    (export_dir / "structure.json").unlink(missing_ok=True)
    sources_dir.mkdir(parents=True, exist_ok=True)
    objects_dir.mkdir(parents=True, exist_ok=True)

    source_links = []
    for document in connection.execute(
        "SELECT id, relative_path, title, content_hash, parser_version FROM documents ORDER BY relative_path"
    ).fetchall():
        version = connection.execute(
            """
            SELECT id FROM document_versions
            WHERE document_id=? AND content_hash=? AND parser_version=? LIMIT 1
            """,
            (document["id"], document["content_hash"], document["parser_version"]),
        ).fetchone()
        headings = connection.execute(
            """
            SELECT heading_path FROM structural_nodes
            WHERE document_version_id=? AND node_type='section' AND heading_path<>''
            ORDER BY ordinal
            """,
            (version["id"],),
        ).fetchall() if version else []
        relative = Path(document["relative_path"] + ".md")
        target = sources_dir / relative
        write_atomic(target, "\n".join([
            f"# {document['title']}", "",
            f"- 原文：`{document['relative_path']}`",
            f"- 内容哈希：`{document['content_hash']}`",
            "", "## 结构", "",
            *([f"- {row['heading_path']}" for row in headings] or ["- 未识别标题结构"]),
            "",
        ]))
        source_links.append(f"- [{document['title']}](sources/{relative.as_posix()})")

    object_links = []
    for obj in connection.execute(
        "SELECT * FROM knowledge_objects ORDER BY canonical_name"
    ).fetchall():
        filename = f"{safe_filename(obj['canonical_name'])}-{obj['object_key'][-6:]}.md"
        terms = connection.execute(
            "SELECT term, term_kind FROM object_terms WHERE object_id=? ORDER BY term_kind, term",
            (obj["id"],),
        ).fetchall()
        sources = connection.execute(
            """
            SELECT d.relative_path FROM object_sources s JOIN documents d ON d.id=s.document_id
            WHERE s.object_id=? ORDER BY d.relative_path
            """,
            (obj["id"],),
        ).fetchall()
        claims = connection.execute(
            """
            SELECT c.predicate, t.canonical_name AS target_name, c.literal_value, c.qualifiers_json
            FROM claims c LEFT JOIN knowledge_objects t ON t.id=c.object_object_id
            WHERE c.subject_object_id=? ORDER BY c.predicate, target_name, c.literal_value
            """,
            (obj["id"],),
        ).fetchall()
        evidence = connection.execute(
            """
            SELECT DISTINCT m.evidence, d.relative_path
            FROM object_mentions m
            JOIN document_versions v ON v.id=m.document_version_id
            JOIN documents d ON d.id=v.document_id
            WHERE m.object_id=? AND m.evidence<>'' ORDER BY d.relative_path LIMIT 8
            """,
            (obj["id"],),
        ).fetchall()
        lines = [
            f"# {obj['canonical_name']}", "",
            f"- 领域类型：`{obj['domain_type']}`",
            f"- 本体类别：`{obj['ontology_kind']}`",
            f"- 状态：`{obj['status']}`",
            "", "## 说明", "", obj["description"] or "暂无说明。",
            "", "## 词汇", "",
            *([f"- {row['term']}（{row['term_kind']}）" for row in terms] or ["- 暂无词汇。"]),
            "", "## 来源", "",
            *([f"- `{row['relative_path']}`" for row in sources] or ["- 暂无来源。"]),
            "", "## Claim", "",
            *([claim_line(row) for row in claims] or ["- 暂无 Claim。"]),
            "", "## 证据", "",
            *([f"- `{row['relative_path']}`：{row['evidence']}" for row in evidence] or ["- 暂无证据。"]),
            "",
        ]
        write_atomic(objects_dir / filename, "\n".join(lines))
        object_links.append(f"- [{obj['canonical_name']}](objects/{filename})")

    write_atomic(export_dir / "index.md", "\n".join([
        "# Knowledge Export", "", "## Objects", "", *(object_links or ["- 暂无对象。"]),
        "", "## Sources", "", *(source_links or ["- 暂无来源。"]), "",
    ]))
    write_atomic(export_dir / "log.md", "\n".join([
        "# Export Log", "", f"- Exported at: {datetime.now().isoformat(timespec='seconds')}",
        f"- Index version: {INDEX_VERSION}", f"- Objects: {len(object_links)}", f"- Sources: {len(source_links)}", "",
    ]))
    connection.close()
    return {"path": str(export_dir), "objects": len(object_links), "sources": len(source_links)}


def claim_line(row):
    target = row["target_name"] or row["literal_value"]
    qualifiers = row["qualifiers_json"] if row["qualifiers_json"] != "{}" else ""
    return f"- {row['predicate']} → {target}" + (f"；限定：`{qualifiers}`" if qualifiers else "")


def clear_markdown(folder):
    if folder.exists():
        for path in folder.rglob("*.md"):
            path.unlink()


def write_atomic(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.replace(temporary, path)


def safe_filename(value):
    name = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", str(value or "").strip()).strip(" .")
    return name[:80] or "untitled"
