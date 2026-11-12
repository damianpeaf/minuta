"""Transcription engines: `local` (free, on this machine) and `vercel` (paid API, your key)."""

from __future__ import annotations

ENGINES = ("local", "vercel")


def price_per_minute(name: str, cfg: dict) -> float:
    if name == "local":
        return 0.0
    if name == "vercel":
        from minuta.transcription.vercel import usd_per_minute

        return usd_per_minute(cfg["transcription"].get("vercel") or {})
    raise ValueError(f"unknown engine {name!r}; known: {', '.join(ENGINES)}")


def window_seconds(name: str, cfg: dict) -> float | None:
    """Longest audio per request for paid engines (None for local)."""
    if name == "vercel":
        from minuta.transcription.common import WINDOW
        from minuta.transcription.vercel import option

        return float(option(cfg["transcription"].get("vercel") or {}, "window_max_seconds", WINDOW) or WINDOW)
    return None


def create_engine(name: str, cfg: dict, secrets=None, on_progress=None):
    """`on_progress(step, fraction, **info)`: the local engine reports "download" while it fetches its model."""
    t = cfg["transcription"]
    if name == "local":
        from minuta.transcription import local

        return local.create(t.get("local") or {}, on_progress=on_progress)
    if name == "vercel":
        from minuta.transcription.vercel import KEY_NAME, VercelEngine

        key = secrets.get(KEY_NAME) if secrets is not None else None
        return VercelEngine(t.get("vercel") or {}, key)
    raise ValueError(f"unknown engine {name!r}; known: {', '.join(ENGINES)}")
