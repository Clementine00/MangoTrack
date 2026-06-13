"""Structured (JSON) logging for MangoTrack.

One JSON object per line on stdout ("JSON Lines"), so the platform (Fly) and any
log tooling can parse each event by field instead of scraping plaintext. The app
only ever writes to stdout — it never manages log files (twelve-factor); Fly
captures stdout for `fly logs`.

A logging Formatter has one job: turn a LogRecord into a string. This one renders
it as JSON. Where the log goes (stdout) and whether it's emitted (level) are the
Handler's and Logger's jobs — configured in setup_logging().
"""
import json
import logging
import os
import sys
from contextvars import ContextVar
from datetime import datetime, timezone

# Holds the current request's id, implicitly scoped to the running async task.
# The HTTP middleware sets it per request; the filter below stamps it onto every
# log record, so all logs emitted while handling one request share an id.
# Default "-" for logs emitted outside any request (startup, migrations).
request_id_var: ContextVar[str] = ContextVar("request_id", default="-")

# Snapshot the attribute names a vanilla LogRecord carries, so we can tell our
# own `extra={...}` fields apart from the library's built-ins. `message` and
# `asctime` are added later during formatting; `taskName` is added by asyncio on
# 3.12+. Anything on a record NOT in this set is a structured field we passed in.
_BUILTIN_ATTRS = set(logging.LogRecord("", 0, "", 0, "", (), None).__dict__) | {
    "message",
    "asctime",
    "taskName",
}


class JsonFormatter(logging.Formatter):
    """Render each log record as a single JSON line."""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            # UTC ISO-8601, matching the timestamps the app writes to the DB.
            "time": datetime.fromtimestamp(record.created, tz=timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            # getMessage() applies %-args, e.g. log.info("tracked %s", id).
            "msg": record.getMessage(),
        }
        # Merge structured fields passed via extra={...} onto the JSON object,
        # so they become real, filterable keys instead of being lost in the text.
        for key, value in record.__dict__.items():
            if key not in _BUILTIN_ATTRS:
                payload[key] = value
        # If logged inside `except ... exc_info=True`, attach the traceback.
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # default=str so a non-JSON value (UUID, datetime) stringifies instead of
        # raising — a logging call must never crash the code that called it.
        return json.dumps(payload, default=str)


class RequestIdFilter(logging.Filter):
    """Stamp every record with the current request id from the contextvar.

    A Filter runs on each record before formatting. This one never drops anything
    (always returns True) — it just attaches request_id so correlation is
    automatic: no caller has to pass it in extra={...}.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        record.request_id = request_id_var.get()
        return True


def setup_logging() -> None:
    """Send JSON logs to stdout at the level named by LOG_LEVEL (default INFO).

    This is pure delivery setup: it wires a stdout handler with the JSON formatter
    onto the ROOT logger and sets the threshold. It does NOT capture anything by
    itself — records only appear when code calls log.x() (or middleware does).
    Configuring root means every logger in the app (mangotrack.*, and libraries)
    funnels through one JSON handler. uvicorn keeps its own handlers, so its
    access logs aren't doubled.
    """
    handler = logging.StreamHandler(sys.stdout)   # stdout, not StreamHandler's default stderr
    handler.setFormatter(JsonFormatter())
    handler.addFilter(RequestIdFilter())          # stamp request_id on every record

    root = logging.getLogger()
    root.handlers.clear()                          # idempotent: avoid duplicate handlers on re-call
    root.addHandler(handler)
    root.setLevel(os.getenv("LOG_LEVEL", "INFO").upper())
