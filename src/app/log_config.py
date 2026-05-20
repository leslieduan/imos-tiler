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
from uvicorn.logging import DefaultFormatter as _UvicornDefaultFormatter

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


class TextFormatter(_UvicornDefaultFormatter):
    """Uvicorn's DefaultFormatter with structured extras appended as ``key=value`` pairs.

    Keeps local-dev parity with the JSON path: the values passed via
    ``extra={...}`` show up in the terminal the same way they show up as JSON
    fields in CloudWatch. Without this, ``message`` carries only the event name
    and all context is invisible in TTY mode.
    """

    def format(self, record: logging.LogRecord) -> str:
        base = super().format(record)
        extras = " ".join(
            f"{key}={record.__dict__[key]}"
            for key in record.__dict__
            if key not in _RESERVED_RECORD_ATTRS
        )
        return f"{base}  {extras}" if extras else base


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
    if _use_json():
        LOGGING_CONFIG["formatters"]["default"] = {"()": JsonFormatter}
        LOGGING_CONFIG["formatters"]["access"] = {"()": JsonFormatter}
    else:
        # Swap uvicorn's DefaultFormatter for our subclass so extras render as
        # key=value after the message. Access formatter unchanged — it already
        # renders client_addr/request_line/status_code via its own fmt string.
        LOGGING_CONFIG["formatters"]["default"] = {
            "()": TextFormatter,
            "fmt": "%(levelprefix)s %(asctime)s %(message)s",
            "datefmt": "%H:%M:%S",
            "use_colors": None,
        }

    # Route all app.* loggers through uvicorn's "default" handler. Registering
    # the "app" namespace as the parent means app.main, app.services.*, and
    # app.routers.* all propagate here and stop (propagate=False prevents
    # further fallthrough to the root logger, which uvicorn leaves unconfigured).
    app_level = os.environ.get("LOG_LEVEL", "INFO").upper()
    LOGGING_CONFIG["loggers"]["app"] = {
        "handlers": ["default"],
        "level": app_level,
        "propagate": False,
    }

    logging.config.dictConfig(LOGGING_CONFIG)
    logging.getLogger("uvicorn.access").addFilter(SuppressHealthChecks())
