"""Find a Chromium-based browser and the extension folder."""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path


@dataclass
class Browser:
    name: str
    exe: str
    extensions_url: str


def _candidates() -> list[tuple[str, str, list[str]]]:
    if sys.platform == "darwin":
        apps = Path("/Applications")
        return [
            ("Google Chrome", "chrome://extensions", [str(apps / "Google Chrome.app/Contents/MacOS/Google Chrome")]),
            ("Microsoft Edge", "edge://extensions", [str(apps / "Microsoft Edge.app/Contents/MacOS/Microsoft Edge")]),
            ("Brave", "brave://extensions", [str(apps / "Brave Browser.app/Contents/MacOS/Brave Browser")]),
            ("Chromium", "chrome://extensions", [str(apps / "Chromium.app/Contents/MacOS/Chromium")]),
        ]
    if os.name == "nt":
        roots = [os.environ.get(v, "") for v in ("PROGRAMFILES", "PROGRAMFILES(X86)", "LOCALAPPDATA")]
        roots = [r for r in roots if r]

        def under(rel: str) -> list[str]:
            return [str(Path(r) / rel) for r in roots]

        return [
            ("Google Chrome", "chrome://extensions", under(r"Google\Chrome\Application\chrome.exe")),
            ("Microsoft Edge", "edge://extensions", under(r"Microsoft\Edge\Application\msedge.exe")),
            ("Brave", "brave://extensions", under(r"BraveSoftware\Brave-Browser\Application\brave.exe")),
        ]
    return [
        ("Google Chrome", "chrome://extensions", ["google-chrome", "google-chrome-stable"]),
        ("Microsoft Edge", "edge://extensions", ["microsoft-edge", "microsoft-edge-stable"]),
        ("Brave", "brave://extensions", ["brave-browser", "brave"]),
        ("Chromium", "chrome://extensions", ["chromium", "chromium-browser"]),
    ]


def detect() -> list[Browser]:
    found = []
    for name, url, cands in _candidates():
        for c in cands:
            exe = c if os.path.isabs(c) else shutil.which(c)
            if exe and os.path.exists(exe):
                found.append(Browser(name, exe, url))
                break
    return found


def open_extensions_page(browser: Browser) -> bool:
    """chrome:// pages cannot be opened through the OS URL handler; the browser must be asked."""
    try:
        subprocess.Popen([browser.exe, browser.extensions_url], stdout=subprocess.DEVNULL,  # noqa: S603
                         stderr=subprocess.DEVNULL, start_new_session=True)
        return True
    except OSError:
        return False


def extension_dir(cfg: dict | None = None) -> Path:
    """The folder to load with "Load unpacked". Prefer the installed checkout (stable across
    updates), then a development checkout, then the copy shipped inside the package."""
    app_dir = ((cfg or {}).get("general") or {}).get("app_dir")
    if app_dir and (Path(app_dir).expanduser() / "extension" / "manifest.json").exists():
        return Path(app_dir).expanduser() / "extension"
    repo = Path(__file__).resolve().parents[2] / "extension"
    if (repo / "manifest.json").exists():
        return repo
    return Path(__file__).resolve().parent / "_extension"
