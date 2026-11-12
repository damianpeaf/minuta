"""Pairing the browser extension with the local server.

- The server token is a random secret in `<config>/server-token` (0600). Every request except
  `/health` and `/pair` must carry it in the `X-Minuta-Token` header.
- `minuta setup` (or `minuta pair`) creates a short one-time code, shown in the terminal and valid
  for 10 minutes. Only its hash is written to disk.
- The user types the code in the extension popup; the extension sends it to `/pair` and receives
  the token once. The extension's origin (`chrome-extension://<id>`) is remembered, and from then on
  requests with any other browser origin are refused.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import threading
import time

from minuta import paths
from minuta.redact import register_secret

CODE_TTL = 600
MAX_ATTEMPTS = 5
TOKEN_HEADER = "X-Minuta-Token"  # noqa: S105 - a header name
EXTENSION_SCHEMES = ("chrome-extension://",)

_lock = threading.Lock()


def _token_file():
    return paths.config_dir() / "server-token"


def get_token(create: bool = False) -> str | None:
    f = _token_file()
    if f.exists():
        token = f.read_text(encoding="utf-8").strip()
        if token:
            register_secret(token)
            return token
    if not create:
        return None
    token = secrets.token_urlsafe(32)
    paths.write_private(f, token + "\n")
    register_secret(token)
    return token


def rotate_token() -> str:
    _token_file().unlink(missing_ok=True)
    state = read_state()
    state["allowed_origins"] = []
    write_state(state)
    return get_token(create=True)  # type: ignore[return-value]


def _hash(code: str) -> str:
    return hashlib.sha256(code.replace("-", "").strip().encode()).hexdigest()


def new_code(ttl: int = CODE_TTL) -> str:
    """A 6-digit code like 482-913. Replaces any previous one."""
    get_token(create=True)
    digits = f"{secrets.randbelow(10**6):06d}"
    with _lock:
        paths.write_private(paths.pairing_file(), json.dumps(
            {"code_sha256": _hash(digits), "expires_at": time.time() + ttl, "attempts": 0}))
    return f"{digits[:3]}-{digits[3:]}"


def redeem(code: str) -> bool:
    """True once for the right code; the code is consumed. Wrong guesses are counted."""
    with _lock:
        p = paths.pairing_file()
        if not p.exists() or not isinstance(code, str):
            return False
        try:
            d = json.loads(p.read_text())
        except ValueError:
            return False
        if d.get("expires_at", 0) < time.time() or d.get("attempts", 0) >= MAX_ATTEMPTS:
            p.unlink(missing_ok=True)
            return False
        if hmac.compare_digest(d.get("code_sha256", ""), _hash(code)):
            p.unlink(missing_ok=True)
            return True
        d["attempts"] = d.get("attempts", 0) + 1
        paths.write_private(p, json.dumps(d))
        return False


def check_token(given: str | None) -> bool:
    token = get_token()
    return bool(token and given) and hmac.compare_digest(token.encode(), given.encode())


# --- state shared between the server and the CLI --------------------------------
def read_state() -> dict:
    p = paths.runtime_file()
    try:
        return json.loads(p.read_text()) if p.exists() else {}
    except ValueError:
        return {}


def write_state(state: dict) -> None:
    with _lock:
        paths.write_private(paths.runtime_file(), json.dumps(state, indent=2))


def update_state(**changes) -> dict:
    s = read_state()
    s.update(changes)
    write_state(s)
    return s


def allow_origin(origin: str) -> None:
    s = read_state()
    allowed = list(dict.fromkeys([*s.get("allowed_origins", []), origin]))
    s["allowed_origins"] = allowed
    s["paired_at"] = time.time()
    write_state(s)


def is_extension_origin(origin: str) -> bool:
    return origin.startswith(EXTENSION_SCHEMES) and len(origin) > len("chrome-extension://")
