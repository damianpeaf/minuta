"""What the CLI prints: tables, a spinner per step, progress bars, paths, dates and durations.

rich is imported only when something is printed with it, so `minuta --version` and the menu stay fast.
Without a terminal (pipes, tests) nothing moves: a step prints one line and bars print nothing.
"""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

from minuta.i18n import language, t

_console = None
MONTHS = {
    "en": ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"],
    "es": ["ene", "feb", "mar", "abr", "may", "jun", "jul", "ago", "sep", "oct", "nov", "dic"],
}


def console():
    """One rich Console, writing to whatever sys.stdout is at the time of each print."""
    global _console
    if _console is None:
        from rich.console import Console
        from rich.theme import Theme

        theme = Theme({"ok": "green", "fail": "red", "warn": "yellow", "dim": "dim", "head": "bold",
                       "accent": "cyan"})
        # Without a terminal rich would wrap at 80 columns and cut paths in two.
        width = None if sys.stdout.isatty() else 200
        _console = Console(theme=theme, highlight=False, width=width)
    return _console


def interactive_output() -> bool:
    return console().is_terminal


def say(text: str = "", style: str | None = None) -> None:
    """Print text as is (no markup, so a meeting title with brackets prints right)."""
    console().print(text, style=style, markup=False, highlight=False, soft_wrap=True)


def markup(text: str) -> None:
    console().print(text, highlight=False, soft_wrap=True)


def esc(text) -> str:
    from rich.markup import escape

    return escape(str(text))


def blank() -> None:
    console().print()


def rule(title: str = "") -> None:
    c = console()
    if c.is_terminal:
        c.rule(f"[head]{esc(title)}[/]" if title else "", style="dim", align="left")
    else:
        c.print(f"-- {title} --" if title else "--", markup=False)


def ok(text: str) -> None:
    markup(f"[ok]{esc(t('mark.ok'))}[/]  {esc(text)}")


def fail(text: str) -> None:
    markup(f"[fail]{esc(t('mark.fail'))}[/]  {esc(text)}")


def warn(text: str) -> None:
    markup(f"[warn]{esc(t('mark.warn'))}[/]  {esc(text)}")


def error(text: str) -> None:
    """A problem the user has to fix, on stderr."""
    print(text, file=sys.stderr)


def path_markup(path) -> str:
    """A path in quotes (spaces and all, ready to paste), clickable where the terminal supports links."""
    p = Path(path)
    try:
        uri = p.resolve().as_uri()
    except ValueError:
        return esc(f'"{p}"')
    return f'[link={uri}]{esc(chr(34) + str(p) + chr(34))}[/link]'


def table(columns: list[str], rows: list[list], *, title: str | None = None):
    """A table with headers. Cells are plain text (escaped) unless they are rich renderables."""
    from rich.table import Table

    tb = Table(title=title, title_justify="left", header_style="bold", box=_box(), pad_edge=False,
               show_edge=False)
    for c in columns:
        tb.add_column(c, overflow="fold")
    for r in rows:
        tb.add_row(*[c if hasattr(c, "__rich_console__") or hasattr(c, "__rich__") else _text(c) for c in r])
    console().print(tb)


def _text(value):
    from rich.text import Text

    return Text("" if value is None else str(value))


def styled(value: str, style: str):
    from rich.text import Text

    return Text(value, style=style)


def _box():
    from rich import box

    return box.SIMPLE_HEAD


def kv(rows: list[tuple[str, object]]) -> None:
    """Two columns, label and value, without headers."""
    from rich.table import Table

    tb = Table.grid(padding=(0, 2))
    tb.add_column(style="dim", no_wrap=True)
    tb.add_column(overflow="fold")
    for k, v in rows:
        tb.add_row(k, v if hasattr(v, "__rich_console__") or hasattr(v, "__rich__") else _text(v))
    console().print(tb)


@contextlib.contextmanager
def step(message: str):
    """A spinner with the step's name while it runs. Without a terminal, one line."""
    c = console()
    if c.is_terminal:
        with c.status(f"{esc(message)}...", spinner="dots", spinner_style="accent"):
            yield
    else:
        c.print(f"{message}...", markup=False)
        yield


class Bar:
    """A progress bar with percentage and time left. `update(done, total)`; `total=None` spins."""

    def __init__(self, description: str, *, unit: str = "", transient: bool = True):
        self.description = description
        self.unit = unit
        self._progress = None
        self._task = None
        self._transient = transient

    def __enter__(self):
        c = console()
        if c.is_terminal:
            from rich.progress import (
                BarColumn,
                DownloadColumn,
                Progress,
                SpinnerColumn,
                TaskProgressColumn,
                TextColumn,
                TimeRemainingColumn,
            )

            cols = [SpinnerColumn(style="accent"), TextColumn("{task.description}"), BarColumn(),
                    TaskProgressColumn()]
            if self.unit == "bytes":
                cols.append(DownloadColumn())
            cols.append(TimeRemainingColumn(compact=True, elapsed_when_finished=True))
            self._progress = Progress(*cols, console=c, transient=self._transient)
            self._progress.start()
            self._task = self._progress.add_task(self.description, total=None)
        else:
            c.print(f"{self.description}...", markup=False)
        return self

    def update(self, done: float, total: float | None = None, description: str | None = None) -> None:
        if self._progress is None:
            return
        kw = {"completed": done, "total": total}
        if description:
            kw["description"] = description
        self._progress.update(self._task, **kw)

    def __exit__(self, *exc):
        if self._progress is not None:
            self._progress.stop()
        return False


# --- formats -------------------------------------------------------------------------------
def local_time(iso: str | None) -> datetime | None:
    from minuta.meetings import parse_time

    when = parse_time(iso)
    return when.astimezone() if when else None


def when(iso: str | None) -> str:
    """'12 nov 2026, 16:50' in local time and the CLI's language."""
    d = local_time(iso)
    if d is None:
        return "-"
    month = MONTHS.get(language(), MONTHS["en"])[d.month - 1]
    return f"{d.day} {month} {d.year}, {d:%H:%M}"


def duration(seconds) -> str:
    """'48 s', '12 min', '1 h 05 min'."""
    if not seconds:
        return "-"
    s = round(float(seconds))
    if s < 60:
        return f"{s} s"
    minutes = round(s / 60)
    if minutes < 60:
        return f"{minutes} min"
    return f"{minutes // 60} h {minutes % 60:02d} min"


def ago(timestamp: float | None) -> str:
    if not timestamp:
        return t("never")
    seconds = max(0, datetime.now(timezone.utc).timestamp() - float(timestamp))
    return t("ago", what=duration(max(1, seconds)))


def clock(seconds: float) -> str:
    s = int(seconds)
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


# --- the desktop --------------------------------------------------------------------------
def open_with_system(path: Path) -> bool:
    """Open a file or folder with the app the system uses for it. Not on a Linux without a desktop
    (over ssh xdg-open would start a browser nobody sees)."""
    if sys.platform.startswith("linux") and not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY")):
        return False
    try:
        if sys.platform == "darwin":
            subprocess.Popen(["open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        elif os.name == "nt":
            os.startfile(str(path))  # noqa: S606 - a path, not a command
        else:
            subprocess.Popen(["xdg-open", str(path)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True)
        return True
    except (OSError, AttributeError):
        return False


def copy_to_clipboard(text: str) -> bool:
    """pbcopy, clip, wl-copy or xclip, whichever exists; False when none does."""
    import shutil

    for argv in (["pbcopy"], ["clip"], ["wl-copy"], ["xclip", "-selection", "clipboard"]):
        exe = shutil.which(argv[0])
        if not exe:
            continue
        try:
            subprocess.run([exe, *argv[1:]], input=text.encode("utf-16-le" if argv[0] == "clip" else "utf-8"),
                           timeout=5, check=True, capture_output=True)
            return True
        except (OSError, subprocess.SubprocessError):
            continue
    return False
