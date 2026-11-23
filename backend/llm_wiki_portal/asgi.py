import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "llm_wiki_portal.settings")

application = get_asgi_application()

from api.maintenance import start_scheduler

start_scheduler()
