import json

from django.core.management.base import BaseCommand, CommandError

from api.projects import get_project, list_projects
from api.search_index import build_project_index


class Command(BaseCommand):
    help = "Build or incrementally update persistent SQLite search indexes for local projects."

    def add_arguments(self, parser):
        parser.add_argument("--project-id", help="Local project id; omit to build every registered local project.")
        parser.add_argument("--force", action="store_true", help="Reparse every document even if its fingerprint is unchanged.")
        parser.add_argument("--incremental", action="store_true", help="Explicitly request the default incremental behavior.")

    def handle(self, *args, **options):
        project_id = options.get("project_id")
        if project_id:
            project = get_project(project_id)
            if not project:
                raise CommandError(f"Unknown local project: {project_id}")
            projects = [project]
        else:
            projects = list_projects()
        if not projects:
            self.stdout.write("No registered local projects.")
            return
        failed = False
        for project in projects:
            result = build_project_index(project, force=options["force"])
            self.stdout.write(json.dumps({"projectId": project["id"], **result}, ensure_ascii=False))
            failed = failed or not result["ok"]
        if failed:
            raise CommandError("One or more indexes completed with document failures.")
