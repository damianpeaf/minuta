"""Talk to the local server from the CLI."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from minuta import pairing


def _get(port: int, path: str, token: str | None = None, timeout: float = 2.0) -> dict | None:
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}")  # noqa: S310 - fixed local URL
    if token:
        req.add_header(pairing.TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310
            return json.loads(r.read() or b"{}")
    except (urllib.error.URLError, OSError, ValueError):
        return None


def health(port: int, timeout: float = 2.0) -> dict | None:
    h = _get(port, "/health", timeout=timeout)
    return h if h and h.get("app") == "minuta" else None


def status(port: int) -> dict | None:
    return _get(port, "/status", pairing.get_token())
