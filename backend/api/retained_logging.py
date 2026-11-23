"""Daily, per-process files: no shared rotation handles on Windows or Linux."""
import json
import logging
import os
import re
import uuid
from contextvars import ContextVar
from pathlib import Path

from django.conf import settings
from django.utils import timezone

log_secrets = ContextVar("log_secrets", default=())
_process_id = uuid.uuid4().hex[:8]


def redact(value):
    text = str(value)
    for secret in (*log_secrets.get(), settings.LLM_API_KEY, os.getenv("WIKI_MCP_TOKEN", "")):
        if secret:
            text = text.replace(str(secret), "[REDACTED]")
    text = re.sub(r"(?i)(bearer\s+)[^\s\"',}]+", r"\1[REDACTED]", text)
    return re.sub(r'''(?i)((?:api[_-]?key|password|token|secret|cookie|authorization)["']?\s*[:=]\s*["']?)[^\s,"'}]+''',
                  r"\1[REDACTED]", text)


class DailyFileHandler(logging.Handler):
    def __init__(self, category="runtime"):
        super().__init__()
        self.category = category

    def emit(self, record):
        try:
            directory = Path(settings.RETAINED_LOG_DIR)
            directory.mkdir(parents=True, exist_ok=True)
            stamp = timezone.localdate().isoformat()
            path = directory / f"{self.category}-{stamp}-{os.getpid()}-{_process_id}.jsonl"
            entry = {"time": timezone.now().isoformat(), "level": record.levelname,
                     "logger": record.name, "message": redact(self.format(record))}
            if hasattr(record, "task_id"):
                entry["taskId"] = str(record.task_id)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except Exception:
            # Never echo record contents (which may contain secrets) on write failure.
            import sys
            sys.stderr.write("Retained log write failed; check log storage permissions/capacity.\n")
