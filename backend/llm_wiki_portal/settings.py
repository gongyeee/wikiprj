from pathlib import Path
import os

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env", override=True)

SECRET_KEY = os.getenv("DJANGO_SECRET_KEY", "dev-only-change-me")
DEBUG = os.getenv("DJANGO_DEBUG", "true").lower() == "true"
ALLOWED_HOSTS = [host.strip() for host in os.getenv("DJANGO_ALLOWED_HOSTS", "127.0.0.1,localhost").split(",") if host.strip()]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "api",
]

MIDDLEWARE = [
    "api.middleware.CorsMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "api.audit.AuditMiddleware",
    "api.middleware.RequireLoginMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "llm_wiki_portal.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "llm_wiki_portal.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": BASE_DIR / "db.sqlite3",
    }
}

LANGUAGE_CODE = "zh-hans"
TIME_ZONE = "Asia/Shanghai"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.openai.com/v1").rstrip("/")
LLM_API_KEY = os.getenv("LLM_API_KEY", "")
LLM_MODEL = os.getenv("LLM_MODEL", "gpt-4.1-mini")
LLM_TIMEOUT = int(os.getenv("LLM_TIMEOUT", "90"))
QUERY_INTENT_TIMEOUT = max(1, int(os.getenv("QUERY_INTENT_TIMEOUT", "15")))
QUERY_INTENT_MAX_TOKENS = max(128, int(os.getenv("QUERY_INTENT_MAX_TOKENS", "1024")))
DOMAIN_LAYER_DIR = BASE_DIR / "data" / "domain_layers"

# Folder uploads use one multipart file per selected document.
DATA_UPLOAD_MAX_NUMBER_FILES = 1000

RETAINED_LOG_DIR = Path(os.getenv("RETAINED_LOG_DIR", str(BASE_DIR / "logs" / "retained")))
MAINTENANCE_BACKUP_DIR = Path(os.getenv("MAINTENANCE_BACKUP_DIR", str(BASE_DIR / "backups" / "retention")))
MAINTENANCE_ENABLED = os.getenv("MAINTENANCE_ENABLED", "true").lower() == "true"
MAINTENANCE_HOUR = int(os.getenv("MAINTENANCE_HOUR", "2"))
LOG_RETENTION_DAYS = int(os.getenv("LOG_RETENTION_DAYS", "90"))
AUDIT_RETENTION_DAYS = int(os.getenv("AUDIT_RETENTION_DAYS", "180"))
if not 0 <= MAINTENANCE_HOUR <= 23 or min(LOG_RETENTION_DAYS, AUDIT_RETENTION_DAYS) < 1:
    raise ValueError("维护时间必须为0–23点，日志和审计留存天数必须大于0")

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {
        "retained": {"class": "api.retained_logging.DailyFileHandler"},
        "tasks": {"class": "api.retained_logging.DailyFileHandler", "category": "tasks"},
    },
    "root": {"handlers": ["retained"], "level": "INFO"},
    "loggers": {
        "api.tasks": {"handlers": ["tasks"], "level": "INFO", "propagate": False},
        "django": {"handlers": ["retained"], "level": "WARNING", "propagate": False},
        "django.server": {"handlers": ["retained"], "level": "WARNING", "propagate": False},
    },
}
