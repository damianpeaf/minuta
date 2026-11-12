"""Where minuta keeps its files.

`MINUTA_HOME` puts config and data under one folder (tests, portable installs). Otherwise the
platform's usual places are used (platformdirs). The data folder can be moved in the config.
"""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_config_dir, user_data_dir, user_log_dir

APP = "minuta"


def _home() -> Path | None:
    h = os.environ.get("MINUTA_HOME")
    return Path(h).expanduser() if h else None


def config_dir() -> Path:
    h = _home()
    return h / "config" if h else Path(user_config_dir(APP, appauthor=False, roaming=True))


def default_data_dir() -> Path:
    h = _home()
    return h / "data" if h else Path(user_data_dir(APP, appauthor=False))


def default_app_dir() -> Path:
    """Where the installers put the source checkout (and the extension folder)."""
    return Path(user_data_dir(APP, appauthor=False)) / "app"


def log_dir() -> Path:
    h = _home()
    return h / "logs" if h else Path(user_log_dir(APP, appauthor=False))


def config_file() -> Path:
    return config_dir() / "config.toml"


def env_file() -> Path:
    return config_dir() / ".env"


def pairing_file() -> Path:
    return config_dir() / "pairing.json"


def runtime_file() -> Path:
    """Server state the CLI reads: last extension heartbeat, paired origins."""
    return config_dir() / "state.json"


def private_dir(path: Path) -> Path:
    """Create a folder only the current user can open (0700 on POSIX), or tighten an existing one."""
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == "posix":
        os.chmod(path, 0o700)
    return path


def write_private(path: Path, text: str) -> None:
    """Write a file only the current user can read (0600 on POSIX), in a folder only they can open."""
    private_dir(path.parent)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(text)
    if os.name == "posix":
        os.chmod(tmp, 0o600)
    os.replace(tmp, path)
