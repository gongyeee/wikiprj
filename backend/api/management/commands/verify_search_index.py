import json

from django.core.management.base import BaseCommand, CommandError

from api.projects import get_project, list_projects
from api.search_index import verify_project_index


class Command(BaseCommand):
    help = "Verify persistent SQLite search indexes for local projects."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", help="Local project id; omit to verify every registered local project.")

    def handle(self, *args, **options):
        project_id = options.get("project_id")
        if project_id:
            project = get_project(project_id)
            if not project:
                raise CommandError(f"Unknown local project: {project_id}")
            projects = [project]
        else:
            projects = list_projects()
        failed = False
        for project in projects:
            result = verify_project_index(project)
            self.stdout.write(json.dumps({"projectId": project["id"], **result}, ensure_ascii=False))
            failed = failed or not result["ok"]
        if failed:
            raise CommandError("One or more indexes failed verification.")
