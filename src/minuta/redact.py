"""Keep secrets out of logs.

Two layers: every secret value minuta reads or writes is registered and replaced verbatim, and
common credential shapes (Bearer tokens, `key=value` pairs with secret-sounding names, our own
pairing header) are masked even if they were never registered.
"""

from __future__ import annotations

import logging
import re
import threading

MASK = "***"
_known: set[str] = set()
_lock = threading.Lock()

_PATTERNS = [
    re.compile(r"(?i)(bearer\s+)[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)(x-minuta-token[\"']?\s*[:=]\s*[\"']?)[A-Za-z0-9._~+/=-]{6,}"),
    re.compile(r"(?i)((?:api[_-]?key|token|secret|password|authorization)[\"']?\s*[:=]\s*[\"']?)[^\s\"',;&]{6,}"),
]


def register_secret(value: str | None) -> None:
    if value and len(value) >= 6:
        with _lock:
            _known.add(value)


def is_secret(value: str | None) -> bool:
    """True if this exact value was registered as a secret."""
    if not value:
        return False
    with _lock:
        return value in _known


def redact(text: str) -> str:
    if not text:
        return text
    with _lock:
        known = sorted(_known, key=len, reverse=True)
    for v in known:
        if v in text:
            text = text.replace(v, MASK)
    for p in _PATTERNS:
        text = p.sub(lambda m: m.group(1) + MASK, text)
    return text


class RedactingFilter(logging.Filter):
    """Attach to handlers: rewrites the final message, arguments included."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            msg = record.getMessage()
        except Exception:  # noqa: BLE001 - a bad format string still gets logged, redacted
            msg = str(record.msg)
        record.msg = redact(msg)
        record.args = None
        if record.exc_info and not record.exc_text:
            record.exc_text = logging.Formatter().formatException(record.exc_info)
        if record.exc_text:
            record.exc_text = redact(record.exc_text)
        return True


def setup_logging(level: int = logging.INFO, logfile=None, stream: bool = True) -> None:
    """Log to stderr (`stream`) and/or a rotating file. The CLI logs only to the file: what the user
    needs to know is printed as a message of its own."""
    handlers: list[logging.Handler] = [logging.StreamHandler()] if stream else []
    if logfile is not None:
        from logging.handlers import RotatingFileHandler

        logfile.parent.mkdir(parents=True, exist_ok=True)
        handlers.append(RotatingFileHandler(logfile, maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in handlers:
        h.setFormatter(fmt)
        h.addFilter(RedactingFilter())
        root.addHandler(h)
    root.setLevel(level)
