"""Talk to the local server from the CLI."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from minuta import pairing


class ServerError(RuntimeError):
    def __init__(self, status: int, body: dict):
        super().__init__(body.get("error") or f"HTTP {status}")
        self.status, self.body = status, body


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


def call(port: int, method: str, path: str, body: dict | None = None, timeout: float = 30.0) -> tuple[int, dict]:
    """A request with the server token. Returns (status, body) for any answer; raises OSError when
    nothing answers."""
    data = json.dumps(body).encode() if body is not None else (b"{}" if method == "POST" else None)
    req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", data=data, method=method)  # noqa: S310
    req.add_header("Content-Type", "application/json")
    token = pairing.get_token()
    if token:
        req.add_header(pairing.TOKEN_HEADER, token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:  # noqa: S310 - fixed local URL
            return r.status, json.loads(r.read() or b"{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b"{}")
        except ValueError:
            return e.code, {}


def service(cfg: dict) -> int | None:
    """The port of the background service when it answers and takes our token: then long actions run
    there as jobs, so the terminal and the extension's page see the same progress. None otherwise."""
    port = int(cfg["server"]["port"])
    if not health(port, timeout=0.5) or not pairing.get_token():
        return None
    try:
        code, body = call(port, "GET", "/jobs", timeout=2)
    except OSError:
        return None
    return port if code == 200 and "jobs" in body else None
