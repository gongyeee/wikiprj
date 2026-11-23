import os

from django.core.wsgi import get_wsgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "llm_wiki_portal.settings")

application = get_wsgi_application()

from api.maintenance import start_scheduler

start_scheduler()
