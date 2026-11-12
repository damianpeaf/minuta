"""Find a Chromium-based browser and the extension folder.

The extension is loaded with "Load unpacked" from a copy in a visible folder without spaces
(`~/minuta-extension`): the source checkout lives in hidden folders (`~/Library/Application Support`)
that the browser's file dialog does not show. `minuta update` keeps the copy current.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

COPY_MARKER = ".minuta-extension"


@dataclass
class Browser:
    name: str
    exe: str
    extensions_url: str


def _candidates() -> list[tuple[str, str, list[str]]]:
    if sys.platform == "darwin":
        def app(name: str) -> list[str]:
            return [str(d / f"{name}.app/Contents/MacOS/{name}") for d in (Path("/Applications"),
                                                                            Path.home() / "Applications")]

        return [
            ("Google Chrome", "chrome://extensions", app("Google Chrome")),
            ("Microsoft Edge", "edge://extensions", app("Microsoft Edge")),
            ("Brave", "brave://extensions", app("Brave Browser")),
            ("Chromium", "chrome://extensions", app("Chromium")),
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
    """The folder to load with "Load unpacked": the visible copy once setup made it, else the source."""
    copy = extension_copy_dir(cfg)
    if (copy / COPY_MARKER).exists() and (copy / "manifest.json").exists():
        return copy
    return extension_source(cfg)


def default_extension_copy_dir() -> Path:
    """`~/minuta-extension` (`%USERPROFILE%\\minuta-extension` on Windows); under MINUTA_HOME when set."""
    home = os.environ.get("MINUTA_HOME")
    return (Path(home).expanduser() if home else Path.home()) / "minuta-extension"


def extension_copy_dir(cfg: dict | None = None) -> Path:
    custom = ((cfg or {}).get("general") or {}).get("extension_dir")
    return Path(custom).expanduser() if custom else default_extension_copy_dir()


def install_extension(cfg: dict | None = None) -> Path:
    """Copy the extension to its visible folder (again: files that went away are removed) and return
    that folder. A folder that exists and was not made by minuta is left alone: the source is used."""
    src = extension_source(cfg)
    dest = extension_copy_dir(cfg)
    if dest.resolve() == src.resolve():
        return src
    if dest.exists() and any(dest.iterdir()) and not (dest / COPY_MARKER).exists():
        return src
    dest.mkdir(parents=True, exist_ok=True)
    wanted = set()
    for f in src.rglob("*"):
        rel = f.relative_to(src)
        wanted.add(rel)
        target = dest / rel
        if f.is_dir():
            target.mkdir(parents=True, exist_ok=True)
        elif not target.exists() or target.read_bytes() != f.read_bytes():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(f, target)
    for f in sorted(dest.rglob("*"), reverse=True):
        rel = f.relative_to(dest)
        if rel in wanted or rel == Path(COPY_MARKER):
            continue
        if f.is_dir():
            shutil.rmtree(f, ignore_errors=True)
        else:
            f.unlink(missing_ok=True)
    (dest / COPY_MARKER).write_text("Made by minuta. `minuta update` replaces what is here.\n", encoding="utf-8")
    return dest


def remove_extension_copy(cfg: dict | None = None) -> bool:
    dest = extension_copy_dir(cfg)
    if (dest / COPY_MARKER).exists():
        shutil.rmtree(dest, ignore_errors=True)
        return True
    return False


@dataclass
class ExtensionSync:
    folder: Path  # the folder to load
    copied: bool  # the copy was refreshed
    old_folder: Path | None = None  # the source folder, when a browser may still load it from there
    old_folder_in_use: bool = False


def sync_extension(cfg: dict) -> ExtensionSync:
    """After an update: refresh the visible copy (making it if it is missing). An extension still loaded
    from the old place keeps working, as the update refreshed that folder too; the user is told."""
    copy = extension_copy_dir(cfg)
    existed = (copy / COPY_MARKER).exists()
    folder = install_extension(cfg)
    if folder != copy:
        return ExtensionSync(folder, False)
    src = extension_source(cfg)
    loaded = loaded_extension_paths()
    from_source = any(_same(p, src) for p in loaded)
    from_copy = any(_same(p, copy) for p in loaded)
    old_in_use = from_source or (not existed and not from_copy)
    return ExtensionSync(copy, True, src if old_in_use else None, old_in_use)


def _same(a: Path, b: Path) -> bool:
    try:
        return a.resolve() == b.resolve()
    except OSError:
        return False


def profile_roots() -> list[Path]:
    """The user data folders of Chrome, Edge, Brave and Chromium."""
    home = Path.home()
    if sys.platform == "darwin":
        base = home / "Library" / "Application Support"
        names = ["Google/Chrome", "Microsoft Edge", "BraveSoftware/Brave-Browser", "Chromium"]
        return [base / n for n in names]
    if os.name == "nt":
        base = Path(os.environ.get("LOCALAPPDATA") or home / "AppData" / "Local")
        names = ["Google/Chrome/User Data", "Microsoft/Edge/User Data", "BraveSoftware/Brave-Browser/User Data",
                 "Chromium/User Data"]
        return [base / n for n in names]
    base = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    return [base / n for n in ("google-chrome", "microsoft-edge", "BraveSoftware/Brave-Browser", "chromium")]


def loaded_extension_paths(roots: list[Path] | None = None) -> list[Path]:
    """Folders of unpacked extensions named minuta that a browser profile loads. Only reads the
    profiles' preference files; a profile that cannot be read is skipped."""
    found: list[Path] = []
    for root in roots if roots is not None else profile_roots():
        if not root.is_dir():
            continue
        for profile in [root / "Default", *sorted(root.glob("Profile *"))]:
            for name in ("Secure Preferences", "Preferences"):
                try:
                    prefs = json.loads((profile / name).read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    continue
                settings = ((prefs.get("extensions") or {}).get("settings") or {})
                for ext in settings.values():
                    path = (ext or {}).get("path") if isinstance(ext, dict) else None
                    if not path or not os.path.isabs(path):
                        continue  # store extensions have a relative path
                    manifest = Path(path) / "manifest.json"
                    try:
                        if json.loads(manifest.read_text(encoding="utf-8")).get("name") == "minuta":
                            found.append(Path(path))
                    except (OSError, ValueError):
                        if Path(path).name in ("extension", "minuta-extension"):
                            found.append(Path(path))
    return found


def extension_source(cfg: dict | None = None) -> Path:
    """Prefer the installed checkout (stable across updates), then a development checkout, then the
    copy shipped inside the package."""
    app_dir = ((cfg or {}).get("general") or {}).get("app_dir")
    if app_dir and (Path(app_dir).expanduser() / "extension" / "manifest.json").exists():
        return Path(app_dir).expanduser() / "extension"
    repo = Path(__file__).resolve().parents[2] / "extension"
    if (repo / "manifest.json").exists():
        return repo
    return Path(__file__).resolve().parent / "_extension"
