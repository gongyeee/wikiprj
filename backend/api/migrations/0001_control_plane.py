import json
import uuid
from pathlib import Path

from django.conf import settings
from django.db import migrations, models
import django.db.models.deletion


PROJECT_NAMESPACE = uuid.UUID("b5d21518-2910-45d4-a942-3ab9b5cd0c62")


def normalized_path(value):
    return str(Path(value).resolve()).casefold()


def import_legacy_control_plane(apps, schema_editor):
    Project = apps.get_model("api", "Project")
    ProjectSourceRoot = apps.get_model("api", "ProjectSourceRoot")
    IngestionJob = apps.get_model("api", "IngestionJob")

    registry_path = Path(settings.BASE_DIR) / "data" / "manual_wiki_projects.json"
    if registry_path.exists():
        try:
            projects = (json.loads(registry_path.read_text(encoding="utf-8")) or {}).get("projects", [])
        except (OSError, json.JSONDecodeError):
            projects = []
        for item in projects:
            root = str(item.get("path") or "").strip()
            if not root:
                continue
            project_id = uuid.uuid5(PROJECT_NAMESPACE, normalized_path(root))
            project, _ = Project.objects.update_or_create(
                id=project_id,
                defaults={
                    "name": item.get("name") or item.get("title") or Path(root).name,
                    "root_path": str(Path(root).resolve()),
                    "wiki_dir": str(Path(item.get("wikiDir") or Path(root) / "wiki").resolve()),
                    "status": "active",
                },
            )
            raw_dir = item.get("rawDir")
            if raw_dir:
                ProjectSourceRoot.objects.get_or_create(
                    project=project,
                    path=str(Path(raw_dir).resolve()),
                    defaults={"is_primary": True},
                )

    jobs_dir = Path(settings.BASE_DIR) / "data" / "wiki_layer_jobs"
    if jobs_dir.exists():
        for job_path in jobs_dir.glob("*.json"):
            try:
                payload = json.loads(job_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            job_id = str(payload.get("id") or job_path.stem)
            if not job_id:
                continue
            project = None
            root = str(payload.get("projectDir") or "").strip()
            if root:
                project = Project.objects.filter(root_path__iexact=str(Path(root).resolve())).first()
            IngestionJob.objects.update_or_create(
                id=job_id,
                defaults={
                    "project": project,
                    "status": payload.get("status") or "unknown",
                    "started_at": float(payload.get("startedAt") or 0),
                    "finished_at": payload.get("finishedAt"),
                    "payload": payload,
                },
            )


class Migration(migrations.Migration):
    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="Project",
            fields=[
                ("id", models.UUIDField(default=uuid.uuid4, editable=False, primary_key=True, serialize=False)),
                ("name", models.CharField(max_length=200)),
                ("root_path", models.TextField(unique=True)),
                ("wiki_dir", models.TextField(blank=True)),
                ("status", models.CharField(default="active", max_length=24)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={"ordering": ("name", "id")},
        ),
        migrations.CreateModel(
            name="IngestionJob",
            fields=[
                ("id", models.CharField(max_length=64, primary_key=True, serialize=False)),
                ("status", models.CharField(db_index=True, max_length=32)),
                ("started_at", models.FloatField(db_index=True)),
                ("finished_at", models.FloatField(blank=True, null=True)),
                ("payload", models.JSONField(default=dict)),
                ("project", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, to="api.project")),
            ],
            options={"ordering": ("-started_at",)},
        ),
        migrations.CreateModel(
            name="ProjectSourceRoot",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("path", models.TextField()),
                ("is_primary", models.BooleanField(default=True)),
                ("include_patterns", models.JSONField(blank=True, default=list)),
                ("exclude_patterns", models.JSONField(blank=True, default=list)),
                ("project", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="source_roots", to="api.project")),
            ],
        ),
        migrations.AddConstraint(
            model_name="projectsourceroot",
            constraint=models.UniqueConstraint(fields=("project", "path"), name="unique_project_source_root"),
        ),
        migrations.RunPython(import_legacy_control_plane, migrations.RunPython.noop),
    ]
