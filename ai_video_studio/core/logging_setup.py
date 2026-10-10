"""Structured logging: JSON lines to a rotating file, readable text to stderr."""

from __future__ import annotations

import json
import logging
import logging.handlers
import re
from datetime import datetime, timezone
from pathlib import Path

LOG_FILE_NAME = "ai_video_studio.log"

# Values of these keys (and anything shaped like a bearer/API key) are never written to logs.
_SECRET_KEYS = re.compile(r"(api[_-]?key|token|secret|password|authorization)", re.IGNORECASE)
_SECRET_VALUES = re.compile(r"(sk-[A-Za-z0-9_\-]{8,}|AIza[0-9A-Za-z_\-]{20,})")
_STANDARD_ATTRS = set(vars(logging.makeLogRecord({}))) | {"message", "asctime"}


def redact(value):
    if isinstance(value, dict):
        return {k: ("***" if _SECRET_KEYS.search(str(k)) else redact(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v) for v in value]
    if isinstance(value, str):
        return _SECRET_VALUES.sub("***", value)
    return value


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "time": datetime.fromtimestamp(record.created, timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": redact(record.getMessage()),
        }
        extra = {k: v for k, v in vars(record).items() if k not in _STANDARD_ATTRS}
        if extra:
            entry["context"] = redact(extra)
        if record.exc_info:
            entry["exception"] = self.formatException(record.exc_info)
        return json.dumps(entry, default=str, ensure_ascii=False)


def configure_logging(log_dir: Path, level: str = "INFO") -> Path:
    log_dir.mkdir(parents=True, exist_ok=True)
    log_path = log_dir / LOG_FILE_NAME
    root = logging.getLogger()
    for handler in list(root.handlers):
        if getattr(handler, "_avs_handler", False):
            root.removeHandler(handler)
            handler.close()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    file_handler = logging.handlers.RotatingFileHandler(
        log_path, maxBytes=5 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    file_handler.setFormatter(JsonFormatter())
    file_handler._avs_handler = True  # type: ignore[attr-defined]

    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
    console._avs_handler = True  # type: ignore[attr-defined]

    root.addHandler(file_handler)
    root.addHandler(console)
    return log_path


def set_level(level: str) -> None:
    logging.getLogger().setLevel(getattr(logging, level.upper(), logging.INFO))
