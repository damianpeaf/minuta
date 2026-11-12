"""config.toml: defaults, loading and saving. Secrets never live here (see `credentials.py`)."""

from __future__ import annotations

import copy
import sys
from pathlib import Path
from typing import Any

import tomli_w

from minuta import paths

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover
    import tomli as tomllib

DEFAULT_PORT = 47321
AUDIO_FORMATS = ("opus", "mp3")
TRANSCRIPTION_MODES = ("on-demand", "auto")
# auto: record every call; ask: the extension asks when a call starts; manual: only from the popup.
RECORDING_MODES = ("auto", "ask", "manual")

DEFAULTS: dict[str, Any] = {
    "general": {
        "language": "en",  # language of the CLI and setup assistant: en | es
        "data_dir": "",  # empty: the platform's data folder
        "audio_format": "opus",  # opus keeps the browser's audio as is; mp3 re-encodes (stereo kept)
        "app_dir": "",  # source checkout used by `minuta update`
        "install_extras": "local",  # extras `minuta update` reinstalls with (local, local,cuda or empty)
        "install_ref": "",  # branch or tag `minuta update` follows (MINUTA_REF at install; empty: default branch)
        "extension_dir": "",  # copy of the browser extension to load; empty: ~/minuta-extension
    },
    "recording": {"mode": "auto"},  # auto | ask | manual
    "speakers": {"me": "Me", "others": "Others"},
    "server": {"port": DEFAULT_PORT},
    "storage": {"delete_local_audio_after_upload": False},
    "delivery": {"max_attempts": 3},
    "destinations": {
        "local": {"type": "local", "enabled": True, "send": ["audio", "transcript", "metadata"]},
    },
    "transcription": {
        "mode": "on-demand",
        "engine": "local",
        "language": "",  # empty: detect. BCP-47 like "en", "es-419"
        "context": "",  # a sentence describing the meetings, in the meeting language
        "glossary": [],  # names and jargon the model should spell right
        "max_usd": 1.0,  # spending cap per run for paid engines
        "fallback_engine": "",  # Spanish only: re-run phrases that came out in another language
        "local": {"model": "auto", "device": "auto"},
        # window_max_seconds and usd_per_minute can be set here; by default each model's preset decides.
        "vercel": {"model": "openai/whisper-1"},
    },
    "secrets": {"backend": "auto"},  # auto: OS keyring with a 0600 .env fallback; file: only the .env
}


def deep_merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in override.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = copy.deepcopy(v)
    return out


def load(path: Path | None = None) -> dict:
    path = path or paths.config_file()
    user: dict = {}
    if path.exists():
        with open(path, "rb") as f:
            user = tomllib.load(f)
    cfg = deep_merge(DEFAULTS, user)
    # A user who defined their own destinations replaces the default list instead of merging into it.
    if "destinations" in user:
        cfg["destinations"] = copy.deepcopy(user["destinations"])
    return cfg


def save(cfg: dict, path: Path | None = None) -> Path:
    path = path or paths.config_file()
    paths.write_private(path, tomli_w.dumps(_clean(cfg)))
    return path


def _clean(v):
    """tomli-w cannot write None: drop those keys."""
    if isinstance(v, dict):
        return {k: _clean(x) for k, x in v.items() if x is not None}
    if isinstance(v, list):
        return [_clean(x) for x in v if x is not None]
    return v


def exists() -> bool:
    return paths.config_file().exists()


def data_dir(cfg: dict) -> Path:
    d = cfg.get("general", {}).get("data_dir")
    return Path(d).expanduser() if d else paths.default_data_dir()


def meetings_dir(cfg: dict) -> Path:
    return data_dir(cfg) / "meetings"


def make_data_dirs(cfg: dict) -> Path:
    """Create the data and meetings folders, private to the current user. Returns the meetings folder."""
    paths.private_dir(data_dir(cfg))
    return paths.private_dir(meetings_dir(cfg))


def validate(cfg: dict) -> list[str]:
    """Problems a human can fix in config.toml. Empty list means fine."""
    errors = []
    g = cfg["general"]
    if g.get("language") not in ("en", "es"):
        errors.append("general.language must be 'en' or 'es'")
    if g.get("audio_format") not in AUDIO_FORMATS:
        errors.append(f"general.audio_format must be one of {AUDIO_FORMATS}")
    port = cfg["server"].get("port")
    if not isinstance(port, int) or not (1024 <= port <= 65535):
        errors.append("server.port must be an integer between 1024 and 65535")
    if (cfg.get("recording") or {}).get("mode") not in RECORDING_MODES:
        errors.append(f"recording.mode must be one of {RECORDING_MODES}")
    if cfg["transcription"].get("mode") not in TRANSCRIPTION_MODES:
        errors.append(f"transcription.mode must be one of {TRANSCRIPTION_MODES}")
    for name, d in (cfg.get("destinations") or {}).items():
        if not isinstance(d, dict) or not d.get("type"):
            errors.append(f"destinations.{name} needs a 'type'")
            continue
        bad = set(d.get("send") or []) - {"audio", "transcript", "metadata"}
        if bad:
            errors.append(f"destinations.{name}.send has unknown items: {sorted(bad)}")
    return errors
