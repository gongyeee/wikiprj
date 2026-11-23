from django.db import migrations


class Migration(migrations.Migration):
    dependencies = [("api", "0002_copy_project_domain_layers")]

    operations = [
        migrations.RenameField(model_name="project", old_name="wiki_dir", new_name="export_dir"),
    ]
