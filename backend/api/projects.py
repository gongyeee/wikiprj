from pathlib import Path

from django.db import transaction

from .models import Project, ProjectSourceRoot
from .search_index import (
    build_project_index, get_indexed_document, get_indexed_structural_node,
    get_project_graph, query_terms, search_project_index,
)
from .targeted_evidence import read_raw_source_text
from .document_versions import scope_project


def list_projects():
    return [project_payload(project) for project in Project.objects.prefetch_related("source_roots").filter(status="active")]


def register_project_directory(path, name=None, raw_dir=None, export_dir=None, build_index=True):
    project_path = Path(path).resolve()
    raw_path = Path(raw_dir).resolve() if raw_dir else default_raw_dir(project_path)
    export_path = Path(export_dir).resolve() if export_dir else (project_path / "exports" / "wiki").resolve()
    with transaction.atomic():
        project = Project.objects.filter(root_path__iexact=str(project_path)).first()
        if project is None:
            project = Project(root_path=str(project_path))
        project.name = name or project_path.name
        project.export_dir = str(export_path)
        project.status = "active"
        project.save()
        ProjectSourceRoot.objects.update_or_create(
            project=project,
            path=str(raw_path),
            defaults={"is_primary": True},
        )
        project.source_roots.exclude(path=str(raw_path)).update(is_primary=False)
    item = project_payload(Project.objects.prefetch_related("source_roots").get(pk=project.pk))
    if build_index:
        try:
            item["searchIndex"] = build_project_index(item)
        except Exception as exc:
            item["searchIndex"] = {"ok": False, "error": str(exc)}
    return item


def get_project(project_id):
    try:
        project = Project.objects.prefetch_related("source_roots").get(pk=project_id, status="active")
    except (Project.DoesNotExist, ValueError):
        return None
    return project_payload(project)


def unregister_project(project_id):
    try:
        Project.objects.filter(pk=project_id).update(status="inactive")
    except ValueError:
        pass
    return list_projects()


def register_ingested_project(job):
    """Commit a chosen project's new location only after the index was published."""
    target_id = (job.get('request') or {}).get('targetProjectId')
    if not target_id:
        return register_project_directory(job['projectDir'], job.get('projectName'), job.get('rawDir'), job.get('exportDir'), build_index=False)
    with transaction.atomic():
        record = Project.objects.select_for_update().get(pk=target_id, status='active')
        original = job['request'].get('originalProjectPath')
        if original and str(Path(record.root_path).resolve()) != str(Path(original).resolve()):
            raise ValueError('摄入期间项目位置已修改，请重新选择项目后重试')
        record.root_path = str(Path(job['projectDir']).resolve())
        record.save(update_fields=['root_path', 'updated_at'])
        return register_project_directory(record.root_path, record.name, job.get('rawDir'), job.get('exportDir'), build_index=False)


def project_payload(project):
    roots = list(project.source_roots.all())
    primary = next((root for root in roots if root.is_primary), roots[0] if roots else None)
    root_path = Path(project.root_path).resolve()
    return {
        "id": str(project.id),
        "name": project.name,
        "title": project.name,
        "path": str(root_path),
        "rawDir": str(Path(primary.path).resolve()) if primary else str(default_raw_dir(root_path)),
        "exportDir": str(Path(project.export_dir).resolve()) if project.export_dir else str((root_path / "exports" / "wiki").resolve()),
    }


def default_raw_dir(root):
    for folder in ("resources", "原文"):
        candidate = root / folder
        if candidate.exists():
            return candidate.resolve()
    return (root / "raw" / "sources").resolve()


def project_root(project):
    return Path(project["path"]).resolve()


def project_export_dir(project):
    return Path(project.get("exportDir") or project_root(project) / "exports" / "wiki").resolve()


def project_raw_dir(project):
    return Path(project.get("rawDir") or default_raw_dir(project_root(project))).resolve()


def is_relative_to(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def resolve_project_path(project, relative_path):
    value = str(relative_path or "").replace("\\", "/").lstrip("/")
    if value.startswith("exports/wiki/"):
        return (project_export_dir(project) / value[len("exports/wiki/"):]).resolve()
    if value.startswith("raw/sources/"):
        return (project_raw_dir(project) / value[len("raw/sources/"):]).resolve()
    return (project_root(project) / value).resolve()


def project_relative_path(project, path):
    path = Path(path).resolve()
    if is_relative_to(path, project_export_dir(project)):
        return f"exports/wiki/{path.relative_to(project_export_dir(project)).as_posix()}"
    if is_relative_to(path, project_raw_dir(project)):
        return f"raw/sources/{path.relative_to(project_raw_dir(project)).as_posix()}"
    return path.relative_to(project_root(project)).as_posix()


def project_file_content(project_id, relative_path, structural_node_id=None):
    project = get_project(project_id)
    if not project:
        return None
    if structural_node_id:
        section = get_indexed_structural_node(project, structural_node_id)
        if section is not None:
            return {**section, "indexed": True}
    cached = get_indexed_document(project, relative_path)
    if cached is not None:
        return {
            "content": cached["extracted_text"],
            "indexed": True,
            "absolutePath": cached["absolute_path"],
            "documentType": cached["document_type"],
        }
    target = resolve_project_path(project, relative_path)
    allowed_roots = (project_root(project), project_export_dir(project), project_raw_dir(project))
    if not any(is_relative_to(target, root) for root in allowed_roots):
        raise ValueError("path 超出项目目录")
    if not target.exists() or not target.is_file():
        raise FileNotFoundError(relative_path)
    if is_relative_to(target, project_raw_dir(project)):
        return {"content": read_raw_source_text(target), "indexed": False}
    return {"content": read_text(target), "indexed": False}


def project_files(project_id):
    project = get_project(project_id)
    if not project:
        return {"items": []}
    files = []
    for base in (project_export_dir(project), project_raw_dir(project)):
        if not base.exists():
            continue
        for path in base.rglob("*"):
            if path.is_file():
                files.append({"path": project_relative_path(project, path), "name": path.name})
    return {"items": files}


def project_query_terms(query):
    return query_terms(query)


def project_search(project_id, payload):
    project = get_project(project_id)
    if not project:
        return {"results": [], "index": {"ok": False, "reason": "项目不存在"}}
    query = str(payload.get("query") or "").strip()
    limit = int(payload.get("limit") or payload.get("topK") or 8)
    from .query_roles import query_roles
    roles = query_roles(payload.get("originalQuery") or query, payload.get("queryIntent"))
    return search_project_index(scope_project(project, payload.get("versionScope")), query, limit, roles)


def project_graph(project_id, version_scope=None):
    project = get_project(project_id)
    if not project:
        return {"nodes": [], "links": []}
    return get_project_graph(scope_project(project, version_scope)) or {"nodes": [], "links": []}


def read_text(path):
    return Path(path).read_text(encoding="utf-8", errors="ignore")
