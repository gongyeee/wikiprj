from django.core.management.base import BaseCommand

from api.evaluation_runner import run_worker


class Command(BaseCommand):
    help = "执行一个已创建的管理员问答评测任务"

    def add_arguments(self, parser):
        parser.add_argument("run_id")

    def handle(self, *args, **options):
        run_worker(options["run_id"])
