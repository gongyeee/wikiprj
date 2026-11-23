import tempfile
from pathlib import Path

from django.test import TestCase

from api.projects import get_project, list_projects, register_project_directory, unregister_project
from api.models import Project, ProjectSourceRoot


class ProjectControlPlaneTests(TestCase):
    def setUp(self):
        Project.objects.all().delete()

    def test_project_registration_uses_database_and_uuid(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            raw = root / "raw" / "sources"
            wiki = root / "wiki"
            raw.mkdir(parents=True)
            wiki.mkdir()
            (wiki / "index.md").write_text("# Wiki", encoding="utf-8")

            project = register_project_directory(root, "本地项目", raw, wiki)

            self.assertEqual(Project.objects.count(), 1)
            self.assertEqual(ProjectSourceRoot.objects.count(), 1)
            self.assertNotIn("manual:", project["id"])
            self.assertEqual(get_project(project["id"])["rawDir"], str(raw.resolve()))
            self.assertEqual(list_projects()[0]["exportDir"], str(wiki.resolve()))

            unregister_project(project["id"])
            self.assertFalse(Project.objects.filter(status="active").exists())
            self.assertIsNone(get_project(project["id"]))
            restored = register_project_directory(root, "恢复登记", raw, wiki, build_index=False)
            self.assertEqual(restored["id"], project["id"])
