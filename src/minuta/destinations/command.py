"""The built-in `command` destination: run a program of yours for each meeting and transcript.

    [destinations.script]
    type = "command"
    command = ["python3", "/home/me/bin/on_meeting.py", "--folder", "{meeting_dir}"]
    timeout_seconds = 300
    pass_env = ["NOTES_DIR"]     # extra variables to pass through (none by default)
    send = ["metadata", "transcript"]

The program runs without a shell (arguments are a list, nothing is interpreted), in the meeting's
folder, with a minimal environment plus MINUTA_EVENT, MINUTA_MEETING_ID and MINUTA_MEETING_DIR.
`{meeting_dir}`, `{meeting_id}` and `{event}` in an argument are replaced; when no argument uses
them, the event and the folder are appended as the last two arguments. A non-zero exit is retried
by the router like any other failure; the last OUTPUT_LIMIT bytes of the output go to the log.
"""

from __future__ import annotations

import logging
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

from minuta.destinations.base import (
    Capabilities,
    CheckResult,
    Destination,
    DestinationError,
    MeetingParts,
    PermanentError,
    Receipt,
)
from minuta.redact import is_secret, redact

log = logging.getLogger(__name__)

OUTPUT_LIMIT = 4000
PLACEHOLDER_RE = re.compile(r"\{(?:meeting_dir|meeting_id|event)\}")
WINDOWS = sys.platform == "win32"
# What a program needs to start and find its tools; nothing that could hold one of minuta's secrets.
BASE_ENV_POSIX = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR")
BASE_ENV_WINDOWS = ("PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP",
                    "USERPROFILE", "USERNAME", "HOMEDRIVE", "HOMEPATH", "APPDATA", "LOCALAPPDATA",
                    "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS",
                    "PROCESSOR_ARCHITECTURE")
BLOCKED_ENV = ("AI_GATEWAY_API_KEY",)
# cmd.exe re-parses the arguments of .bat/.cmd files, which defeats passing them as a list.
WINDOWS_SHELL_SCRIPTS = (".bat", ".cmd")


def parse_command(value) -> list[str]:
    """A TOML list, or a command line split like a shell would (no shell runs)."""
    if isinstance(value, (list, tuple)):
        argv = [str(a) for a in value]
    elif isinstance(value, str):
        argv = shlex.split(value, posix=not WINDOWS)
        if WINDOWS:
            argv = [a[1:-1] if len(a) > 1 and a[0] == a[-1] == '"' else a for a in argv]
    else:
        argv = []
    if not argv or not argv[0].strip():
        raise ValueError("command is empty")
    return argv


def _names(value) -> list[str]:
    if not value:
        return []
    if isinstance(value, str):
        value = value.split(",")
    return [str(v).strip() for v in value if str(v).strip()]


class CommandDestination(Destination):
    """Run a program of yours for each meeting and transcript (no shell)."""

    kind = "command"
    capabilities = Capabilities(transcript=True, metadata=True)

    # --- options ---------------------------------------------------------------
    @property
    def argv(self) -> list[str]:
        return parse_command(self.options.get("command"))

    @property
    def timeout(self) -> float:
        return self.number("timeout_seconds", 300, 1, 86400)

    def environment(self, extra: dict[str, str] | None = None) -> dict[str, str]:
        """The child's whole environment: a short allowlist, `pass_env`, and our variables."""
        source = {k.upper(): v for k, v in os.environ.items()} if WINDOWS else dict(os.environ)
        wanted = list(BASE_ENV_WINDOWS if WINDOWS else BASE_ENV_POSIX)
        wanted += [n.upper() if WINDOWS else n for n in _names(self.options.get("pass_env"))]
        env = {}
        for name in wanted:
            if name.upper().startswith("MINUTA_") or name.upper() in BLOCKED_ENV:
                continue
            value = source.get(name)
            if value is None or is_secret(value):
                continue
            env[name] = value
        env.update(extra or {})
        return env

    def resolve(self, argv: list[str]) -> str:
        """Absolute path of the program, looked up on the child's PATH."""
        exe = os.path.expanduser(argv[0])
        found = exe if os.path.isabs(exe) and os.path.isfile(exe) else shutil.which(
            exe, path=self.environment().get("PATH"))
        if not found:
            raise PermanentError(f"program not found: {argv[0]}")
        if WINDOWS and Path(found).suffix.lower() in WINDOWS_SHELL_SCRIPTS:
            raise PermanentError(".bat and .cmd files are not allowed (cmd.exe would re-read the arguments); "
                                 "use an .exe, `python script.py` or `powershell -File script.ps1`")
        return found

    @classmethod
    def form(cls, name: str, known: dict, secrets=None, **kw) -> list:
        """Asks for the program unless `command` (or `argv`) was given."""
        from minuta.forms import Field

        if known.get("command") or known.get("argv"):
            return []
        return [Field("command", "text", "setup.command", required=True)]

    def check(self) -> CheckResult:
        try:
            argv = self.argv
            self.timeout  # noqa: B018 - validates
            exe = self.resolve(argv)
        except (ValueError, PermanentError) as e:
            return CheckResult(False, str(e))
        return CheckResult(True, f"runs {Path(exe).name} with {len(argv) - 1} argument(s)")

    # --- running ---------------------------------------------------------------
    def build(self, event: str, meeting) -> tuple[list[str], dict[str, str]]:
        argv = self.argv
        exe = self.resolve(argv)
        values = {"{meeting_dir}": str(meeting.dir), "{meeting_id}": meeting.id, "{event}": event}
        args = argv[1:]
        if any(PLACEHOLDER_RE.search(a) for a in args):
            # One pass, so a folder name that contains "{event}" is not replaced again.
            args = [PLACEHOLDER_RE.sub(lambda m: values[m.group(0)], a) for a in args]
        else:
            args = [*args, event, str(meeting.dir)]
        env = self.environment({"MINUTA_EVENT": event, "MINUTA_MEETING_ID": meeting.id,
                                "MINUTA_MEETING_DIR": str(meeting.dir), "MINUTA_DESTINATION": self.name})
        return [exe, *args], env

    def run(self, event: str, meeting) -> Receipt:
        try:
            argv, env = self.build(event, meeting)
            timeout = self.timeout
        except ValueError as e:
            raise PermanentError(str(e)) from e
        started = time.monotonic()
        code, out, err, timed_out = _run(argv, env, Path(meeting.dir), timeout, OUTPUT_LIMIT)
        took = time.monotonic() - started
        name = Path(argv[0]).name
        if out.strip():
            log.info("%s: %s stdout: %s", self.name, name, redact(out.strip()))
        if err.strip():
            log.info("%s: %s stderr: %s", self.name, name, redact(err.strip()))
        if timed_out:
            raise DestinationError(f"{name} did not finish within {timeout:g} s and was stopped")
        if code != 0:
            tail = redact(err.strip() or out.strip())[-300:]
            raise DestinationError(f"{name} exited with {code}" + (f": {tail}" if tail else ""))
        return Receipt(extra={"exit_code": code, "seconds": round(took, 2)})

    def send_meeting(self, meeting, parts: MeetingParts, previous: Receipt | None = None) -> Receipt:
        return self.run(parts.event, meeting)


class _Tail:
    """Reads a pipe to the end, keeping only the last `limit` bytes."""

    def __init__(self, pipe, limit: int):
        self.data = bytearray()
        self.limit = limit
        self.thread = threading.Thread(target=self._read, args=(pipe,), daemon=True)
        self.thread.start()

    def _read(self, pipe) -> None:
        try:
            while chunk := pipe.read(8192):
                self.data += chunk
                if len(self.data) > self.limit:
                    del self.data[: len(self.data) - self.limit]
        except (OSError, ValueError):
            pass

    def text(self) -> str:
        self.thread.join(timeout=5)
        return bytes(self.data).decode("utf-8", errors="replace")


def _run(argv: list[str], env: dict, cwd: Path, timeout: float, limit: int) -> tuple[int, str, str, bool]:
    kwargs: dict = {}
    if WINDOWS:
        kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    else:
        kwargs["start_new_session"] = True  # its own process group, so a timeout stops its children too
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            cwd=str(cwd), env=env, shell=False, **kwargs)
    out, err = _Tail(proc.stdout, limit), _Tail(proc.stderr, limit)
    timed_out = False
    try:
        proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        proc.wait()
    return proc.returncode, out.text(), err.text(), timed_out


def _kill_tree(proc: subprocess.Popen) -> None:
    try:
        if WINDOWS:
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(proc.pid)], capture_output=True, check=False,
                           creationflags=subprocess.CREATE_NO_WINDOW)
        else:
            os.killpg(proc.pid, signal.SIGKILL)
    except (OSError, ProcessLookupError):
        pass
    try:
        proc.kill()
    except OSError:
        pass
