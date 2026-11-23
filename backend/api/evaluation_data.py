"""Project-scoped gold cases and transactional, reviewable bulk changes."""
import hashlib
import re
import uuid
from datetime import timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from .models import EvaluationCase, EvaluationDraft, Project
from .projects import get_project
from .search_index import connect_index

FIELDS = {
    "category": "问题分类", "scene": "问答场景",
    "question": "问题", "expected": "预期答案", "keywords": "问题关键词",
    "reference_names": "引用文档名称", "reference_paths": "引用文档路径",
    "kind": "用例类型", "rubric": "评分要点", "status": "状态",
}
MAX_ROWS = 2000


def question_key(question):
    return hashlib.sha256(re.sub(r"\s+", " ", question).strip().casefold().encode()).hexdigest()


def validate_case(data):
    clean = {key: str(data.get(key) or "").strip() for key in FIELDS}
    clean["kind"] = {"普通问答": "answer", "无答案/应拒答": "refusal", "应拒答": "refusal"}.get(clean["kind"], clean["kind"]) or "answer"
    clean["status"] = {"启用": "enabled", "停用": "disabled", "待核对": "pending"}.get(clean["status"], clean["status"]) or "enabled"
    if clean["kind"] not in {"answer", "refusal"} or clean["status"] not in {"enabled", "disabled", "pending"}:
        raise ValueError("用例类型或状态无效")
    if not clean["question"]:
        raise ValueError("问题不能为空")
    if clean["kind"] == "answer" and not clean["expected"]:
        raise ValueError("普通问答的预期答案不能为空")
    if any(len(value) > 100000 for value in clean.values()):
        raise ValueError("单字段不能超过 100000 字符")
    return clean


def case_payload(case):
    return {"id": str(case.id), "project_id": str(case.project_id), "revision": case.revision,
            **case.data, "created_by": case.created_by, "updated_by": case.updated_by,
            "updated_at": case.updated_at.isoformat()}


def filtered_cases(project_id, filters):
    qs = EvaluationCase.objects.filter(project_id=project_id)
    for field in ("category", "scene", "status", "kind"):
        if filters.get(field):
            qs = qs.filter(**{f"data__{field}": filters[field]})
    if filters.get("q"):
        q = str(filters["q"]).strip()
        qs = qs.filter(Q(data__question__icontains=q) | Q(data__expected__icontains=q) | Q(data__keywords__icontains=q))
    return qs


def selected_cases(project_id, payload):
    qs = filtered_cases(project_id, payload.get("filters") or {})
    if not payload.get("all_filtered"):
        ids = payload.get("ids") or []
        if not ids or len(ids) > MAX_ROWS:
            raise ValueError("请选择问答对（单批最多 2000 条）")
        qs = qs.filter(pk__in=ids)
        if qs.count() != len(set(ids)):
            raise ValueError("所选记录已变化或不属于当前项目，请刷新")
    if not qs.exists() or qs.count() > MAX_ROWS:
        raise ValueError("单批处理 1～2000 条，请缩小筛选范围")
    return qs


def document_catalog(project_id):
    project = get_project(str(project_id))
    connection = connect_index(project) if project else None
    if not connection:
        return []
    try:
        return [dict(row) for row in connection.execute("SELECT virtual_path, relative_path, title FROM documents WHERE status='indexed'")]
    finally:
        connection.close()


def normalized_path(value):
    return value.strip().strip('《》"').replace("\\", "/").strip("/").casefold()


def match_references(data, catalog):
    paths = [x.strip() for x in data.get("reference_paths", "").splitlines() if x.strip()]
    names = [x.strip() for x in data.get("reference_names", "").splitlines() if x.strip()]
    references = paths or names
    matched, unresolved = [], []
    for i, reference in enumerate(references):
        needle = normalized_path(reference)
        candidates = [d for d in catalog if needle in {normalized_path(d["virtual_path"]), normalized_path(d["relative_path"])}]
        if not candidates and names:
            name = normalized_path(names[i] if i < len(names) else names[0] if len(names) == 1 else reference)
            candidates = [d for d in catalog if name in {normalized_path(d["relative_path"]).rsplit("/", 1)[-1], normalized_path(d["title"])}]
        if len(candidates) == 1:
            matched.append(candidates[0]["virtual_path"])
        else:
            unresolved.append(reference)
    return {"matched": sorted(set(matched)), "unresolved": unresolved, "verified": bool(references) and not unresolved}


def make_draft(user, action, payload):
    EvaluationDraft.objects.filter(expires_at__lt=timezone.now()).delete()
    draft = EvaluationDraft.objects.create(owner_id=user.pk, action=action, payload=payload, expires_at=timezone.now() + timedelta(hours=1))
    return str(draft.pk)


def import_preview(user, project_id, rows, conflict="skip"):
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_ROWS:
        raise ValueError("每批需有 1～2000 行")
    if conflict not in {"skip", "update"}:
        raise ValueError("重复处理方式无效")
    projects = list(Project.objects.filter(status="active"))
    catalogs, seen, plan = {}, set(), []
    batch_id = str(uuid.uuid4())
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            plan.append({"row": index, "sheet": "批量录入", "action": "error", "error": "每行必须为问答对象"})
            continue
        entry = {"row": raw.get("_row", index), "sheet": raw.get("_sheet", "批量录入")}
        try:
            if raw.get("_error"):
                raise ValueError(raw["_error"])
            project_value = str(raw.get("project") or project_id).strip()
            matches = [p for p in projects if str(p.id) == project_value or p.name == project_value]
            if len(matches) != 1:
                raise ValueError("所属项目不存在或名称不唯一，请使用项目 ID")
            pid = str(matches[0].pk)
            data = validate_case(raw)
            data["import_batch"] = batch_id
            key = question_key(data["question"])
            if (pid, key) in seen:
                raise ValueError("本批次内重复问题，请合并后导入")
            seen.add((pid, key))
            existing = EvaluationCase.objects.filter(project_id=pid, question_key=key).first()
            if pid not in catalogs:
                catalogs[pid] = document_catalog(pid)
            data["reference_check"] = match_references(data, catalogs[pid])
            entry.update(project_id=pid, project_name=matches[0].name, data=data,
                         question_key=key, existing_id=str(existing.id) if existing else None,
                         revision=existing.revision if existing else None,
                         action=("update" if conflict == "update" else "skip") if existing else "create")
        except (ValueError, TypeError) as exc:
            entry.update(action="error", error=str(exc))
        plan.append(entry)
    counts = {action: sum(e["action"] == action for e in plan) for action in ("create", "update", "skip", "error")}
    draft_id = make_draft(user, "import", {"plan": plan, "counts": counts})
    return {"draft_id": draft_id, "rows": plan, "counts": counts}


@transaction.atomic
def commit_draft(user, draft_id):
    draft = EvaluationDraft.objects.select_for_update().get(pk=draft_id, owner_id=user.pk)
    if draft.committed_at:
        return {"counts": draft.payload["counts"], "already_committed": True}
    if draft.expires_at <= timezone.now():
        raise ValueError("预览已过期，请重新预览")
    plan = draft.payload["plan"]
    for entry in plan:
        if entry["action"] in {"error", "skip"}:
            continue
        if draft.action == "import":
            existing = EvaluationCase.objects.filter(project_id=entry["project_id"], question_key=entry["question_key"]).first()
            if (str(existing.id) if existing else None) != entry["existing_id"] or (existing.revision if existing else None) != entry["revision"]:
                raise ValueError("预览后数据已变更，请重新预览；本次未写入任何记录")
            if existing:
                existing.data = entry["data"]
                existing.revision += 1
                existing.updated_by = user.username
                existing.save()
            else:
                EvaluationCase.objects.create(project_id=entry["project_id"], question_key=entry["question_key"], data=entry["data"], created_by=user.username, updated_by=user.username)
        else:
            source = EvaluationCase.objects.get(pk=entry["id"], project_id=draft.payload["source"])
            if source.revision != entry["revision"] or EvaluationCase.objects.filter(project_id=draft.payload["target"], question_key=source.question_key).exists():
                raise ValueError("迁移预览后数据已变更，请重新预览；本次未写入任何记录")
            if draft.action == "copy":
                EvaluationCase.objects.create(project_id=draft.payload["target"], question_key=source.question_key, data=entry["data"], created_by=user.username, updated_by=user.username)
            else:
                source.project_id = draft.payload["target"]
                source.data = entry["data"]
                source.updated_by = user.username
                source.revision += 1
                source.save()
    draft.committed_at = timezone.now()
    draft.save(update_fields=["committed_at"])
    return {"counts": draft.payload["counts"]}


def transfer_preview(user, project_id, payload):
    target = Project.objects.get(pk=payload.get("target"), status="active")
    if str(target.pk) == str(project_id):
        raise ValueError("目标项目不能与源项目相同")
    action = payload.get("action")
    if action not in {"move", "copy"}:
        raise ValueError("请选择移动或复制")
    catalog = document_catalog(target.pk)
    plan = []
    for case in selected_cases(project_id, payload):
        data = dict(case.data)
        data["reference_check"] = match_references(data, catalog)
        data["transfer"] = {"from_project": str(project_id), "from_case": str(case.id), "at": timezone.now().isoformat(), "action": action}
        if not data["reference_check"]["verified"]:
            data["status"] = "pending"
        conflict = EvaluationCase.objects.filter(project=target, question_key=case.question_key).exists()
        plan.append({"id": str(case.id), "revision": case.revision, "data": data, "action": "skip" if conflict else action})
    counts = {"total": len(plan), "skip": sum(e["action"] == "skip" for e in plan), "pending": sum(e["action"] != "skip" and e["data"]["status"] == "pending" for e in plan)}
    counts[action] = len(plan) - counts["skip"]
    draft_id = make_draft(user, action, {"plan": plan, "counts": counts, "source": str(project_id), "target": str(target.id)})
    return {"draft_id": draft_id, "rows": plan, "counts": counts, "target_name": target.name}
