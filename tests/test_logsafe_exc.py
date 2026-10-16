"""Secrets in exception messages and stack info are scrubbed too, not just the log message."""

import io
import logging

from job_copilot.logsafe import RedactFilter

TOKEN = "123456789:AA" + "x" * 33  # Telegram-bot-token-shaped, without the URL's "bot" prefix


def _logger():
    out = io.StringIO()
    handler = logging.StreamHandler(out)
    handler.addFilter(RedactFilter())
    log = logging.getLogger("test.logsafe_exc")
    log.handlers[:] = [handler]
    log.propagate = False
    return log, out


def test_token_in_exception_traceback_is_redacted():
    log, out = _logger()
    try:
        raise RuntimeError(f"telegram said no for {TOKEN}")
    except RuntimeError:
        log.exception("send failed")
    text = out.getvalue()
    assert "RuntimeError" in text and "[redacted]" in text
    assert TOKEN not in text and "AAxxxx" not in text


def test_bot_url_in_chained_exception_is_redacted():
    log, out = _logger()
    try:
        try:
            raise OSError(f"https://api.telegram.org/bot{TOKEN}/sendMessage")
        except OSError as e:
            raise RuntimeError("wrapped") from e
    except RuntimeError:
        log.error("failed", exc_info=True)
    assert TOKEN not in out.getvalue() and "[redacted]" in out.getvalue()


def test_stack_info_is_redacted():
    rec = logging.LogRecord("x", logging.ERROR, __file__, 1, "m", (), None, sinfo=f"Stack: token={TOKEN}")
    RedactFilter().filter(rec)
    assert TOKEN not in rec.stack_info and "[redacted]" in rec.stack_info
