import hashlib
import json
import shutil
from pathlib import Path

from django.conf import settings
from django.db import migrations


def layer_key(project_id):
    return hashlib.sha1(str(project_id).encode("utf-8")).hexdigest()[:16]


def copy_domain_layers(apps, schema_editor):
    Project = apps.get_model("api", "Project")
    registry_path = Path(settings.BASE_DIR) / "data" / "manual_wiki_projects.json"
    if not registry_path.exists():
        return
    try:
        items = (json.loads(registry_path.read_text(encoding="utf-8")) or {}).get("projects", [])
    except (OSError, json.JSONDecodeError):
        return

    layers_root = Path(settings.DOMAIN_LAYER_DIR)
    for item in items:
        root = str(item.get("path") or "").strip()
        legacy_id = str(item.get("id") or "").strip()
        if not root or not legacy_id:
            continue
        project = Project.objects.filter(root_path__iexact=str(Path(root).resolve())).first()
        source = layers_root / layer_key(legacy_id)
        if not project or not source.exists():
            continue
        target = layers_root / layer_key(project.id)
        target.mkdir(parents=True, exist_ok=True)
        for path in source.iterdir():
            if path.is_file() and not (target / path.name).exists():
                shutil.copy2(path, target / path.name)


class Migration(migrations.Migration):
    dependencies = [("api", "0001_control_plane")]

    operations = [
        migrations.RunPython(copy_domain_layers, migrations.RunPython.noop),
    ]
