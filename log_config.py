"""Logging configuration for the tile server.

Call ``configure_logging()`` once at startup (after ``load_dotenv()``) to wire
up formatters, application-namespace loggers, and the health-check filter.

Format selection (``LOG_FORMAT`` env var):

* unset — auto: JSON when stdout is not a TTY (containers, EC2, CI),
  human-readable when it is (local dev terminal). No config needed in either env.
* ``json``  — force JSON regardless of TTY state.
* ``text``  — force human-readable regardless of TTY state (e.g. docker run -it).

Structured fields:

* Pass values via ``extra={"key": value, ...}`` rather than ``%s``-interpolating
  into the message — they become top-level JSON fields and are queryable in
  CloudWatch Logs Insights (``filter product_id = "SST"``).

Other log-related env vars (defined in their respective modules):
* ``SLOW_FETCH_THRESHOLD_SECONDS`` — services/loader.py
"""

import json
import logging
import logging.config
import os
import sys
from datetime import UTC, datetime

from uvicorn.config import LOGGING_CONFIG

# Standard LogRecord attributes. Anything in record.__dict__ outside this set is
# treated as a user-supplied extra and promoted to a top-level JSON field.
_RESERVED_RECORD_ATTRS = frozenset(
    {
        "args",
        "asctime",
        "created",
        "exc_info",
        "exc_text",
        "filename",
        "funcName",
        "levelname",
        "levelno",
        "lineno",
        "message",
        "module",
        "msecs",
        "msg",
        "name",
        "pathname",
        "process",
        "processName",
        "relativeCreated",
        "stack_info",
        "thread",
        "threadName",
        "taskName",
        # uvicorn occasionally tags records with this for terminal colorisation;
        # never useful as a JSON field.
        "color_message",
    }
)


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object.

    Promotes any non-stdlib attribute on the record to a top-level JSON field.
    Captures:
      * extras passed via ``logger.info("event", extra={...})``
      * fields uvicorn sets on access records (client_addr, request_line,
        status_code) — picked up by the same generic path rather than being
        special-cased.
    """

    def format(self, record: logging.LogRecord) -> str:
        out: dict = {
            "time": datetime.fromtimestamp(record.created, tz=UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key, value in record.__dict__.items():
            if key in _RESERVED_RECORD_ATTRS or key in out:
                continue
            out[key] = value
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        return json.dumps(out, default=str)


class SuppressHealthChecks(logging.Filter):
    """Drop GET /health entries from the uvicorn access log (load-balancer noise)."""

    def filter(self, record: logging.LogRecord) -> bool:
        # request_line from uvicorn: "GET /path HTTP/1.1"
        parts = getattr(record, "request_line", "").split()
        return len(parts) < 2 or parts[1] != "/health"


def _use_json() -> bool:
    """Return True when JSON log format should be used.

    Explicit override via LOG_FORMAT takes priority; otherwise auto-detect from
    whether stdout is a TTY (terminal → human-readable, container/CI → JSON).
    """
    explicit = os.environ.get("LOG_FORMAT", "").lower()
    if explicit == "json":
        return True
    if explicit == "text":
        return False
    return not sys.stdout.isatty()


def configure_logging() -> None:
    """Apply logging config. Must be called after load_dotenv()."""
    LOGGING_CONFIG["formatters"]["default"]["fmt"] = "%(levelprefix)s %(asctime)s %(message)s"
    LOGGING_CONFIG["formatters"]["default"]["datefmt"] = "%H:%M:%S"
    if _use_json():
        LOGGING_CONFIG["formatters"]["default"] = {"()": JsonFormatter}
        LOGGING_CONFIG["formatters"]["access"] = {"()": JsonFormatter}

    # Route application loggers through uvicorn's "default" handler so all app
    # logs share one format and destination. Without this, loggers outside
    # "services.*" (e.g. "main", "routers.admin.products") fall through to the
    # root logger which has no handlers configured by uvicorn's LOGGING_CONFIG.
    app_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    for namespace in ("services", "routers", "main"):
        LOGGING_CONFIG["loggers"][namespace] = {
            "handlers": ["default"],
            "level": app_level,
            "propagate": False,
        }

    logging.config.dictConfig(LOGGING_CONFIG)
    logging.getLogger("uvicorn.access").addFilter(SuppressHealthChecks())
