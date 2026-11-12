"""API keys and tokens.

Stored in the OS keyring (Keychain, Credential Manager, Secret Service). When no keyring works, or
with `[secrets] backend = "file"`, they go to `.env` in the config folder with 0600 permissions.
An environment variable with the same name always wins, so CI and scripts need no storage at all.

Every value read or written is registered with `redact`, so it never reaches a log.
"""

from __future__ import annotations

import logging
import os
import re

from minuta import paths
from minuta.redact import register_secret

SERVICE = "minuta"
log = logging.getLogger(__name__)

NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,63}$")


def _check_name(name: str) -> None:
    if not NAME_RE.match(name):
        raise ValueError(f"secret names are UPPER_SNAKE_CASE: {name!r}")


def _read_env_file() -> dict[str, str]:
    p = paths.env_file()
    if not p.exists():
        return {}
    out = {}
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def _write_env_file(values: dict[str, str]) -> None:
    lines = ["# minuta secrets. Only your user can read this file."]
    lines += [f"{k}={v}" for k, v in sorted(values.items())]
    paths.write_private(paths.env_file(), "\n".join(lines) + "\n")


class SecretStore:
    def __init__(self, backend: str = "auto"):
        self.backend = backend
        self._keyring = None
        if backend == "auto" and not os.environ.get("MINUTA_NO_KEYRING"):
            self._keyring = _usable_keyring()

    @property
    def where(self) -> str:
        return "keyring" if self._keyring else str(paths.env_file())

    def get(self, name: str) -> str | None:
        _check_name(name)
        value = os.environ.get(name)
        if not value and self._keyring:
            try:
                value = self._keyring.get_password(SERVICE, name)
            except Exception:  # noqa: BLE001 - a locked or broken keyring falls back to the file
                log.warning("keyring read failed, trying the .env file")
        if not value:
            value = _read_env_file().get(name)
        if value:
            register_secret(value)
        return value or None

    def set(self, name: str, value: str) -> str:
        """Returns where it was stored."""
        _check_name(name)
        if not value:
            raise ValueError("empty secret")
        register_secret(value)
        if self._keyring:
            try:
                self._keyring.set_password(SERVICE, name, value)
                # Do not leave an older copy behind in the file.
                values = _read_env_file()
                if values.pop(name, None) is not None:
                    _write_env_file(values)
                return "keyring"
            except Exception:  # noqa: BLE001
                log.warning("keyring write failed, storing in the .env file instead")
        values = _read_env_file()
        values[name] = value
        _write_env_file(values)
        return str(paths.env_file())

    def delete(self, name: str) -> None:
        _check_name(name)
        if self._keyring:
            try:
                self._keyring.delete_password(SERVICE, name)
            except Exception:  # noqa: S110 - absent is fine
                pass
        values = _read_env_file()
        if values.pop(name, None) is not None:
            _write_env_file(values)


def _usable_keyring():
    try:
        import keyring
        from keyring.backends import fail

        kr = keyring.get_keyring()
        if isinstance(kr, fail.Keyring) or "null" in type(kr).__module__ or kr.priority < 1:
            return None
        return kr
    except Exception:  # noqa: BLE001
        return None


def store(cfg: dict | None = None) -> SecretStore:
    backend = ((cfg or {}).get("secrets") or {}).get("backend", "auto")
    return SecretStore(backend)
