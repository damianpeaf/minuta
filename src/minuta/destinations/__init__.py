"""Destination registry.

Every adapter, built-in or not, is an entry point in the `minuta.destinations` group
(`kind = "package.module:Class"`); the built-in ones are declared in minuta's pyproject.toml.
See docs/destinations.md.
"""

from __future__ import annotations

import logging
import re
from importlib.metadata import entry_points

from minuta.destinations.base import (
    PARTS,
    Capabilities,
    CheckResult,
    Destination,
    DestinationError,
    MeetingParts,
    PermanentError,
    Receipt,
)

log = logging.getLogger(__name__)

__all__ = [
    "PARTS", "Capabilities", "CheckResult", "Destination", "DestinationError", "MeetingParts",
    "PermanentError", "Receipt", "registry", "create", "store_secrets", "forget_secrets",
]

# A destination's name is a config key and the start of its secrets' names.
NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")

_registry: dict[str, type[Destination]] | None = None


def registry() -> dict[str, type[Destination]]:
    global _registry
    if _registry is None:
        found: dict[str, type[Destination]] = {}
        # minuta's own first: a plugin cannot take over a built-in kind.
        for ep in sorted(entry_points(group="minuta.destinations"), key=lambda e: not e.value.startswith("minuta.")):
            if ep.name in found:
                continue
            try:
                found[ep.name] = ep.load()
            except Exception:  # noqa: BLE001 - one broken plugin must not break the rest
                log.exception("could not load destination %s", ep.name)
        _registry = found
    return dict(_registry)


def create(name: str, options: dict, secrets=None) -> Destination:
    kind = options.get("type")
    cls = registry().get(kind)
    if cls is None:
        raise KeyError(f"unknown destination type {kind!r} for {name!r}; known: {sorted(registry())}")
    return cls(name, options, secrets)


def store_secrets(name: str, cls: type[Destination], opts: dict, store) -> dict:
    """Move every secret option out of `opts` into the secret store, leaving `<option>_secret = NAME`."""
    for secret in cls.secret_options:
        value = opts.pop(secret, None)
        if value:
            key = f"{name}_{secret}".upper().replace("-", "_")
            store.set(key, value)
            opts[f"{secret}_secret"] = key
    return opts


def forget_secrets(opts: dict, store) -> None:
    """Delete the secrets a destination's options point to."""
    for k, v in (opts or {}).items():
        if k.endswith("_secret") and isinstance(v, str):
            try:
                store.delete(v)
            except ValueError:
                pass
