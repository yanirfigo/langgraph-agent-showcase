"""Process setup for the entry points: private file modes and logs that never contain secrets."""

from __future__ import annotations

import logging
import os
import re

# Telegram tokens with or without the URL's "bot" prefix, Anthropic keys, LangSmith keys.
_SECRETS = re.compile(r"bot\d+:[A-Za-z0-9_-]+|\b\d{6,}:[A-Za-z0-9_-]{30,}|sk-ant-[A-Za-z0-9_-]+|lsv2_[A-Za-z0-9_]+")


class RedactFilter(logging.Filter):
    """Backstop: scrub anything token-shaped from every log record, tracebacks included."""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        clean = _SECRETS.sub("[redacted]", msg)
        if clean != msg:
            record.msg, record.args = clean, ()
        if record.exc_info and not record.exc_text:
            # Format the traceback now (as Formatter would) so the scrubbed text is what gets printed.
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = _SECRETS.sub("[redacted]", record.exc_text)
        if record.stack_info:
            record.stack_info = _SECRETS.sub("[redacted]", record.stack_info)
        return True


def setup_process() -> None:
    os.umask(0o077)  # databases, checkpoints and logs we create are private to this user
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    # HTTP client loggers print full request URLs at INFO, and the Telegram token is in the URL.
    for name in ("httpx", "httpcore", "httpx2", "httpcore2", "anthropic", "langsmith"):
        logging.getLogger(name).setLevel(logging.WARNING)
    for handler in logging.getLogger().handlers:
        handler.addFilter(RedactFilter())
